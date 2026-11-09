# Build the same fixture against either checkout's actual context implementation.
# Invoke from the checkout root with PROBE_SOURCE and PROBE_OUTPUT overridden
# when linking a baseline checkout. The production Makefile stays authoritative.
include Makefile

PROBE_SOURCE ?= experiments/issue-809/performance/support/native-context-probe.c
PROBE_OUTPUT ?= experiments/issue-809/performance/support/native-context-probe.exe
PROBE_OBJECT = $(PROBE_OUTPUT:.exe=.o)

$(PROBE_OUTPUT): $(PROBE_OBJECT) GIT-LDFLAGS $(GITLIBS)
	$(QUIET_LINK)$(CC) $(ALL_CFLAGS) -o $@ $(ALL_LDFLAGS) $(filter %.o,$^) $(LIBS)

$(PROBE_OBJECT): $(PROBE_SOURCE) GIT-CFLAGS
	$(QUIET_CC)$(CC) $(ALL_CFLAGS) -c -o $@ $<
