CXX ?= g++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra

all: slime-dj-analyzer

slime-dj-analyzer: slime_dj_analyzer.cpp
	$(CXX) $(CXXFLAGS) -o $@ $<

clean:
	rm -f slime-dj-analyzer

.PHONY: all clean
