// Native port of the slime_audio_dj analyze_track DSP.
//
// Reads a 16-bit PCM WAV (already decoded by ffmpeg upstream) and emits the
// same analysis JSON the pure-Python implementation produces: RMS envelope
// energy/loudness, autocorrelation BPM + beat offset, Krumhansl-profile key
// estimation via Goertzel chroma, and phrase-aligned structure windows.
//
// The algorithms intentionally mirror scripts/slime_audio_dj.py line for line
// (including Python's round-half-even semantics and tuple tie-breaking) so
// cached analyses stay comparable regardless of which implementation ran.
//
// Build: make -C native

#include <algorithm>
#include <cfenv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <optional>
#include <set>
#include <string>
#include <tuple>
#include <vector>

namespace {

constexpr int kFrameMs = 46;

// Python round() is round-half-even; nearbyint honors the default FE_TONEAREST
// mode, which is also half-even.
double python_round(double value, int digits) {
    const double scale = std::pow(10.0, digits);
    return std::nearbyint(value * scale) / scale;
}

struct WavData {
    int sample_rate = 0;
    int channels = 0;
    std::vector<int16_t> mono;  // 0.5/0.5 downmix, matching audioop.tomono
};

bool read_wav_mono(const std::string &path, WavData &out, std::string &error) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        error = "cannot open wav: " + path;
        return false;
    }
    char riff[4];
    uint32_t riff_size = 0;
    char wave[4];
    file.read(riff, 4);
    file.read(reinterpret_cast<char *>(&riff_size), 4);
    file.read(wave, 4);
    if (!file || std::memcmp(riff, "RIFF", 4) != 0 || std::memcmp(wave, "WAVE", 4) != 0) {
        error = "not a RIFF/WAVE file: " + path;
        return false;
    }
    uint16_t audio_format = 0;
    uint16_t channels = 0;
    uint32_t sample_rate = 0;
    uint16_t bits_per_sample = 0;
    std::vector<char> data;
    while (file) {
        char chunk_id[4];
        uint32_t chunk_size = 0;
        file.read(chunk_id, 4);
        file.read(reinterpret_cast<char *>(&chunk_size), 4);
        if (!file) {
            break;
        }
        if (std::memcmp(chunk_id, "fmt ", 4) == 0) {
            std::vector<char> fmt(chunk_size);
            file.read(fmt.data(), chunk_size);
            if (chunk_size >= 16) {
                std::memcpy(&audio_format, fmt.data(), 2);
                std::memcpy(&channels, fmt.data() + 2, 2);
                std::memcpy(&sample_rate, fmt.data() + 4, 4);
                std::memcpy(&bits_per_sample, fmt.data() + 14, 2);
            }
        } else if (std::memcmp(chunk_id, "data", 4) == 0) {
            data.resize(chunk_size);
            file.read(data.data(), chunk_size);
        } else {
            file.seekg(chunk_size + (chunk_size & 1), std::ios::cur);
            continue;
        }
        if (chunk_size & 1) {
            file.seekg(1, std::ios::cur);
        }
    }
    if (audio_format != 1 || bits_per_sample != 16) {
        error = "expected 16-bit PCM wav after decode: " + path;
        return false;
    }
    if (channels == 0 || sample_rate == 0) {
        error = "invalid wav header: " + path;
        return false;
    }
    out.sample_rate = static_cast<int>(sample_rate);
    out.channels = static_cast<int>(channels);
    const size_t frame_count = data.size() / (2 * channels);
    out.mono.resize(frame_count);
    const int16_t *samples = reinterpret_cast<const int16_t *>(data.data());
    if (channels == 1) {
        std::memcpy(out.mono.data(), samples, frame_count * 2);
    } else {
        // audioop.tomono computes l*0.5 + r*0.5 in double, clamps, and
        // truncates toward zero. Extra channels beyond 2 are ignored by the
        // Python path too (tomono only handles stereo; ffmpeg emits stereo).
        for (size_t index = 0; index < frame_count; ++index) {
            const double left = samples[index * channels];
            const double right = samples[index * channels + 1];
            double value = 0.5 * left + 0.5 * right;
            value = std::max(-32768.0, std::min(32767.0, value));
            out.mono[index] = static_cast<int16_t>(value);
        }
    }
    return true;
}

double chunk_rms(const int16_t *samples, size_t count) {
    if (count == 0) {
        return 0.0;
    }
    double total = 0.0;
    for (size_t index = 0; index < count; ++index) {
        total += static_cast<double>(samples[index]) * samples[index];
    }
    return std::sqrt(total / static_cast<double>(count));
}

void rms_envelope(const std::vector<int16_t> &mono, int rate, std::vector<double> &envelope, double &whole_rms) {
    const size_t frame_samples = std::max<size_t>(1, static_cast<size_t>(rate) * kFrameMs / 1000);
    double total_square = 0.0;
    size_t total_samples = 0;
    for (size_t offset = 0; offset < mono.size(); offset += frame_samples) {
        const size_t count = std::min(frame_samples, mono.size() - offset);
        const double rms = chunk_rms(mono.data() + offset, count);
        envelope.push_back(rms);
        total_square += rms * rms * static_cast<double>(count);
        total_samples += count;
    }
    whole_rms = total_samples ? std::sqrt(total_square / static_cast<double>(total_samples)) : 0.0;
}

struct BpmResult {
    std::optional<double> bpm;
    std::optional<int> beat_offset_ms;
    double confidence = 0.0;
};

BpmResult estimate_bpm(const std::vector<double> &envelope) {
    BpmResult result;
    if (envelope.size() < 80) {
        return result;
    }
    double mean = 0.0;
    for (double value : envelope) {
        mean += value;
    }
    mean /= static_cast<double>(envelope.size());
    std::vector<double> centered(envelope.size());
    for (size_t index = 0; index < envelope.size(); ++index) {
        centered[index] = std::max(0.0, envelope[index] - mean);
    }
    std::vector<double> onsets(envelope.size(), 0.0);
    for (size_t index = 1; index < centered.size(); ++index) {
        onsets[index] = std::max(0.0, centered[index] - centered[index - 1]);
    }
    if (*std::max_element(onsets.begin(), onsets.end()) <= 0.0) {
        return result;
    }
    const int min_lag = std::max(1, static_cast<int>(std::nearbyint(60000.0 / 200.0 / kFrameMs)));
    const int max_lag = std::max(min_lag + 1, static_cast<int>(std::nearbyint(60000.0 / 60.0 / kFrameMs)));
    // Python max() over (score, lag) tuples: ties resolve to the larger lag.
    double best_score = -1.0;
    int best_lag = min_lag;
    std::vector<double> scores;
    scores.reserve(max_lag - min_lag + 1);
    for (int lag = min_lag; lag <= max_lag; ++lag) {
        double score = 0.0;
        for (size_t index = lag; index < onsets.size(); ++index) {
            score += onsets[index] * onsets[index - lag];
        }
        scores.push_back(score);
        if (score > best_score || (score == best_score && lag > best_lag)) {
            best_score = score;
            best_lag = lag;
        }
    }
    if (best_score <= 0.0) {
        return result;
    }
    double bpm = 60000.0 / (static_cast<double>(best_lag) * kFrameMs);
    while (bpm < 80.0) {
        bpm *= 2.0;
    }
    while (bpm > 160.0) {
        bpm /= 2.0;
    }
    std::sort(scores.begin(), scores.end(), std::greater<double>());
    const double runner_up = scores.size() > 1 ? scores[1] : 0.0;
    const double confidence = std::min(1.0, std::max(0.0, (best_score - runner_up) / best_score));
    double onset_total = 0.0;
    for (double value : onsets) {
        onset_total += value;
    }
    const double threshold = (onset_total / static_cast<double>(onsets.size())) * 1.5;
    size_t first_peak = 0;
    for (size_t index = 0; index < onsets.size(); ++index) {
        if (onsets[index] >= threshold) {
            first_peak = index;
            break;
        }
    }
    result.bpm = python_round(bpm, 2);
    result.beat_offset_ms = static_cast<int>(first_peak * kFrameMs);
    result.confidence = python_round(confidence, 3);
    return result;
}

double goertzel_power(const std::vector<double> &samples, int rate, double frequency) {
    if (samples.empty()) {
        return 0.0;
    }
    const double omega = 2.0 * M_PI * frequency / rate;
    const double coeff = 2.0 * std::cos(omega);
    double q0 = 0.0, q1 = 0.0, q2 = 0.0;
    for (double sample : samples) {
        q0 = coeff * q1 - q2 + sample;
        q2 = q1;
        q1 = q0;
    }
    return q1 * q1 + q2 * q2 - coeff * q1 * q2;
}

constexpr double kMajorProfile[12] = {6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88};
constexpr double kMinorProfile[12] = {6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17};

double correlation(const double *a, const double *b) {
    double a_mean = 0.0, b_mean = 0.0;
    for (int index = 0; index < 12; ++index) {
        a_mean += a[index];
        b_mean += b[index];
    }
    a_mean /= 12.0;
    b_mean /= 12.0;
    double numerator = 0.0, a_den = 0.0, b_den = 0.0;
    for (int index = 0; index < 12; ++index) {
        numerator += (a[index] - a_mean) * (b[index] - b_mean);
        a_den += (a[index] - a_mean) * (a[index] - a_mean);
        b_den += (b[index] - b_mean) * (b[index] - b_mean);
    }
    a_den = std::sqrt(a_den);
    b_den = std::sqrt(b_den);
    return (a_den != 0.0 && b_den != 0.0) ? numerator / (a_den * b_den) : 0.0;
}

struct KeyResult {
    std::optional<int> tonic;
    std::optional<std::string> mode;
    double confidence = 0.0;
};

KeyResult estimate_key(const std::vector<int16_t> &mono, int rate) {
    KeyResult result;
    const int max_seconds = 90;
    const size_t max_samples = std::min(mono.size(), static_cast<size_t>(rate) * max_seconds);
    const size_t stride = std::max<size_t>(1, static_cast<size_t>(rate) / 4000);
    std::vector<double> samples;
    samples.reserve(max_samples / stride + 1);
    for (size_t index = 0; index < max_samples; index += stride) {
        samples.push_back(static_cast<double>(mono[index]) / 32768.0);
    }
    const double effective_rate = static_cast<double>(rate) / static_cast<double>(stride);
    double chroma[12] = {0.0};
    for (int midi = 36; midi < 85; ++midi) {
        const double frequency = 440.0 * std::pow(2.0, (midi - 69) / 12.0);
        if (frequency >= effective_rate / 2.0) {
            continue;
        }
        chroma[midi % 12] += goertzel_power(samples, static_cast<int>(effective_rate), frequency);
    }
    double total = 0.0;
    for (double value : chroma) {
        total += value;
    }
    if (total == 0.0) {
        return result;
    }
    for (double &value : chroma) {
        value /= total;
    }
    // Python sorts (score, tonic, mode) tuples descending: score first, then
    // tonic, then mode string ("minor" > "major").
    struct Candidate {
        double score;
        int tonic;
        int mode_rank;  // minor=1 > major=0 for descending string comparison
    };
    std::vector<Candidate> candidates;
    for (int tonic = 0; tonic < 12; ++tonic) {
        double rotated_major[12];
        double rotated_minor[12];
        for (int index = 0; index < 12; ++index) {
            rotated_major[index] = kMajorProfile[((index - tonic) % 12 + 12) % 12];
            rotated_minor[index] = kMinorProfile[((index - tonic) % 12 + 12) % 12];
        }
        candidates.push_back({correlation(chroma, rotated_major), tonic, 0});
        candidates.push_back({correlation(chroma, rotated_minor), tonic, 1});
    }
    std::sort(candidates.begin(), candidates.end(), [](const Candidate &a, const Candidate &b) {
        if (a.score != b.score) return a.score > b.score;
        if (a.tonic != b.tonic) return a.tonic > b.tonic;
        return a.mode_rank > b.mode_rank;
    });
    const double runner_up = candidates.size() > 1 ? candidates[1].score : 0.0;
    result.tonic = candidates[0].tonic;
    result.mode = candidates[0].mode_rank == 1 ? "minor" : "major";
    result.confidence = python_round(std::min(1.0, std::max(0.0, candidates[0].score - runner_up)), 3);
    return result;
}

struct StructureWindow {
    std::string kind;
    int start_ms;
    int end_ms;
    double confidence;
    std::string reason;
};

double percentile(std::vector<double> values, double fraction) {
    if (values.empty()) {
        return 0.0;
    }
    std::sort(values.begin(), values.end());
    const long index = std::lround(std::nearbyint((values.size() - 1) * fraction));
    const long clamped = std::min<long>(values.size() - 1, std::max<long>(0, index));
    return values[clamped];
}

std::vector<double> smooth(const std::vector<double> &values, int window) {
    if (values.empty() || window <= 1) {
        return values;
    }
    const int half = window / 2;
    std::vector<double> smoothed(values.size());
    for (int index = 0; index < static_cast<int>(values.size()); ++index) {
        const int start = std::max(0, index - half);
        const int end = std::min<int>(values.size(), index + half + 1);
        double total = 0.0;
        for (int i = start; i < end; ++i) {
            total += values[i];
        }
        smoothed[index] = total / (end - start);
    }
    return smoothed;
}

int align_to_phrase(int ms, std::optional<int> beat_offset_ms, std::optional<int> phrase_ms) {
    if (!beat_offset_ms.has_value() || !phrase_ms.has_value()) {
        return std::max(0, ms);
    }
    if (ms <= *beat_offset_ms) {
        return std::max(0, *beat_offset_ms);
    }
    const double phrases = std::nearbyint(static_cast<double>(ms - *beat_offset_ms) / *phrase_ms);
    return std::max(0, *beat_offset_ms + static_cast<int>(phrases) * *phrase_ms);
}

std::vector<StructureWindow> detect_structure_windows(
    const std::vector<double> &envelope,
    std::optional<double> bpm,
    std::optional<int> beat_offset_ms,
    double duration_s) {
    std::vector<StructureWindow> windows;
    if (envelope.empty() || duration_s <= 0.0) {
        return windows;
    }
    std::optional<int> phrase_ms_opt;
    if (bpm.has_value() && *bpm > 0.0) {
        phrase_ms_opt = static_cast<int>(std::llround((60000.0 / *bpm) * 32));
    }
    const int phrase_ms = phrase_ms_opt.value_or(16000);
    const int min_window_ms = std::max(4000, phrase_ms / 2);
    const int max_ms = static_cast<int>(duration_s * 1000.0);
    const double high = std::max(percentile(envelope, 0.95), 1.0);
    std::vector<double> normalized(envelope.size());
    for (size_t index = 0; index < envelope.size(); ++index) {
        normalized[index] = std::min(1.0, envelope[index] / high);
    }
    const int window_frames = std::max(3, static_cast<int>(std::nearbyint(static_cast<double>(min_window_ms) / kFrameMs)));
    const std::vector<double> energy = smooth(normalized, window_frames);
    const double low_threshold = std::max(0.10, percentile(energy, 0.25));
    const double high_threshold = std::max(low_threshold + 0.10, percentile(energy, 0.72));

    auto add = [&](const std::string &kind, int start_ms, int end_ms, double confidence, const std::string &reason) {
        start_ms = std::max(0, std::min(max_ms, align_to_phrase(start_ms, beat_offset_ms, phrase_ms_opt)));
        int aligned_end = align_to_phrase(end_ms, beat_offset_ms, phrase_ms_opt);
        if (aligned_end == 0) {
            aligned_end = end_ms;
        }
        end_ms = std::max(start_ms + min_window_ms, std::min(max_ms, aligned_end));
        end_ms = std::min(max_ms, end_ms);
        if (end_ms - start_ms >= 1000) {
            windows.push_back({kind, start_ms, end_ms, python_round(std::max(0.0, std::min(1.0, confidence)), 3), reason});
        }
    };

    const int intro_end = std::min(max_ms, max_ms < phrase_ms * 4 ? phrase_ms : phrase_ms * 2);
    if (intro_end > 0) {
        add("intro", 0, intro_end, 0.55, "opening phrase region");
    }
    const int outro_start = std::max(0, max_ms - intro_end);
    if (outro_start > 0) {
        add("outro", outro_start, max_ms, 0.5, "ending phrase region");
    }

    for (size_t index = 1; index + 1 < energy.size(); ++index) {
        const double previous = energy[index - 1];
        const double current = energy[index];
        const double nxt = energy[index + 1];
        if (previous < low_threshold && current < low_threshold && nxt > current) {
            const int start_ms = static_cast<int>(index * kFrameMs);
            add("breakdown", start_ms, start_ms + min_window_ms, 0.6 + (low_threshold - current), "sustained lower-energy section");
        }
        const double rise = nxt - previous;
        if (current < high_threshold && rise > 0.08) {
            const int start_ms = static_cast<int>(index * kFrameMs);
            add("build", start_ms, start_ms + min_window_ms, 0.55 + rise, "energy rising into a likely transition");
        }
        if (current >= high_threshold && previous < high_threshold) {
            const int start_ms = static_cast<int>(index * kFrameMs);
            add("drop", start_ms, start_ms + min_window_ms, 0.65 + (current - high_threshold), "energy crosses high threshold");
        }
    }

    bool has_build = false;
    for (const auto &window : windows) {
        if (window.kind == "build") {
            has_build = true;
            break;
        }
    }
    if (!has_build && static_cast<int>(energy.size()) > window_frames + 1) {
        double strongest_rise = 0.0;
        int rise_index = 0;
        for (int index = 0; index + window_frames < static_cast<int>(energy.size()); ++index) {
            const double rise = energy[index + window_frames] - energy[index];
            if (rise > strongest_rise) {
                strongest_rise = rise;
                rise_index = index;
            }
        }
        if (strongest_rise > 0.04) {
            const int start_ms = rise_index * kFrameMs;
            add("build", start_ms, start_ms + min_window_ms, 0.55 + strongest_rise, "strongest sustained energy rise");
        }
    }

    std::stable_sort(windows.begin(), windows.end(), [](const StructureWindow &a, const StructureWindow &b) {
        if (a.start_ms != b.start_ms) return a.start_ms < b.start_ms;
        if (a.kind != b.kind) return a.kind < b.kind;
        return a.confidence > b.confidence;
    });
    std::vector<StructureWindow> deduped;
    std::set<std::pair<std::string, int>> seen;
    for (const auto &window : windows) {
        const std::pair<std::string, int> bucket{window.kind, window.start_ms / std::max(1, phrase_ms)};
        if (seen.count(bucket)) {
            continue;
        }
        seen.insert(bucket);
        deduped.push_back(window);
        if (deduped.size() >= 24) {
            break;
        }
    }
    return deduped;
}

std::string json_escape(const std::string &value) {
    std::string out;
    for (char c : value) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\t': out += "\\t"; break;
            default: out += c;
        }
    }
    return out;
}

std::string number_or_null(std::optional<double> value) {
    if (!value.has_value()) {
        return "null";
    }
    char buffer[64];
    std::snprintf(buffer, sizeof(buffer), "%.10g", *value);
    return buffer;
}

}  // namespace

int main(int argc, char **argv) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: %s <decoded.wav>\n", argv[0]);
        return 2;
    }
    std::fesetround(FE_TONEAREST);
    WavData wav;
    std::string error;
    if (!read_wav_mono(argv[1], wav, error)) {
        std::fprintf(stderr, "%s\n", error.c_str());
        return 1;
    }
    const double duration_s = wav.sample_rate ? static_cast<double>(wav.mono.size()) / wav.sample_rate : 0.0;
    std::vector<double> envelope;
    double whole_rms = 0.0;
    rms_envelope(wav.mono, wav.sample_rate, envelope, whole_rms);
    const BpmResult bpm = estimate_bpm(envelope);
    const KeyResult key = estimate_key(wav.mono, wav.sample_rate);
    const double loudness_db = 20.0 * std::log10(std::max(whole_rms, 1.0) / 32768.0);
    std::optional<double> bpm_value = bpm.bpm;
    std::optional<int> beat_offset = bpm.beat_offset_ms;
    const auto structure = detect_structure_windows(envelope, bpm_value, beat_offset, duration_s);

    std::string out = "{";
    char buffer[128];
    std::snprintf(buffer, sizeof(buffer), "\"duration_s\": %.3f", python_round(duration_s, 3));
    out += buffer;
    std::snprintf(buffer, sizeof(buffer), ", \"sample_rate\": %d, \"channels\": %d", wav.sample_rate, wav.channels);
    out += buffer;
    out += ", \"bpm\": " + number_or_null(bpm_value);
    out += ", \"beat_offset_ms\": " + (beat_offset.has_value() ? std::to_string(*beat_offset) : std::string("null"));
    out += ", \"tonic\": " + (key.tonic.has_value() ? std::to_string(*key.tonic) : std::string("null"));
    out += ", \"mode\": " + (key.mode.has_value() ? "\"" + *key.mode + "\"" : std::string("null"));
    std::snprintf(buffer, sizeof(buffer), ", \"energy\": %.4f", python_round(std::min(1.0, whole_rms / 32768.0), 4));
    out += buffer;
    std::snprintf(buffer, sizeof(buffer), ", \"loudness_db\": %.2f", python_round(loudness_db, 2));
    out += buffer;
    std::snprintf(buffer, sizeof(buffer), ", \"confidence\": {\"bpm\": %.3f, \"key\": %.3f}", bpm.confidence, key.confidence);
    out += buffer;
    out += ", \"structure\": [";
    for (size_t index = 0; index < structure.size(); ++index) {
        const auto &window = structure[index];
        if (index) {
            out += ", ";
        }
        std::snprintf(
            buffer,
            sizeof(buffer),
            "{\"kind\": \"%s\", \"start_ms\": %d, \"end_ms\": %d, \"confidence\": %.3f, ",
            json_escape(window.kind).c_str(),
            window.start_ms,
            window.end_ms,
            window.confidence);
        out += buffer;
        out += "\"reason\": \"" + json_escape(window.reason) + "\"}";
    }
    out += "]}";
    std::printf("%s\n", out.c_str());
    return 0;
}
