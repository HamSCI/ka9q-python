"""Classification unit tests for scripts/check_upstream_drift.py.

These exercise the pure-Python diff/classify layer without touching git.
The git-integration path is verified by running the script against the
real ka9q-radio checkout (see CI / smoke tests).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make scripts/ importable
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from check_upstream_drift import (  # noqa: E402
    FieldChange, HeaderDelta,
    STREAM_CRITICAL_STATUS_TYPES,
    _aggregate_severity, _classify_change, _diff_enum,
)


# ---------------------------------------------------------------------------
# _classify_change
# ---------------------------------------------------------------------------

class TestClassifyChange:
    def test_added_field_is_warn(self):
        sev, _ = _classify_change("StatusType", "added", "OUTPUT_SSRC")
        assert sev == "warn"

    def test_added_encoding_is_warn(self):
        sev, _ = _classify_change("Encoding", "added", "FOO")
        assert sev == "warn"

    def test_removed_stream_critical_status_is_fail(self):
        sev, reason = _classify_change("StatusType", "removed", "OUTPUT_SSRC")
        assert sev == "fail"
        assert "RTP" in reason or "channel" in reason.lower()

    def test_value_changed_stream_critical_status_is_fail(self):
        sev, _ = _classify_change("StatusType", "value_changed", "OUTPUT_ENCODING")
        assert sev == "fail"

    def test_removed_non_critical_status_is_warn(self):
        sev, _ = _classify_change("StatusType", "removed", "TP1")
        assert sev == "warn"

    def test_value_changed_non_critical_status_is_warn(self):
        sev, _ = _classify_change("StatusType", "value_changed", "FILTER_DROPS")
        assert sev == "warn"

    def test_encoding_value_shift_is_fail(self):
        sev, _ = _classify_change("Encoding", "value_changed", "F32LE")
        assert sev == "fail"

    def test_demod_value_shift_is_fail(self):
        sev, _ = _classify_change("DemodType", "value_changed", "FM_DEMOD")
        assert sev == "fail"

    def test_window_change_is_only_warn(self):
        # WindowType isn't stream-critical for RTP delivery.
        sev, _ = _classify_change("WindowType", "value_changed", "KAISER_WINDOW")
        assert sev == "warn"
        sev, _ = _classify_change("WindowType", "removed", "HANN_WINDOW")
        assert sev == "warn"


# ---------------------------------------------------------------------------
# _diff_enum
# ---------------------------------------------------------------------------

class TestDiffEnum:
    def test_no_changes(self):
        pin = [("A", 0, ""), ("B", 1, "")]
        assert _diff_enum("StatusType", pin, pin) == []

    def test_added_field(self):
        pin  = [("A", 0, "")]
        head = [("A", 0, ""), ("OUTPUT_SSRC", 1, "")]
        out = _diff_enum("StatusType", pin, head)
        assert len(out) == 1
        assert out[0].kind == "added"
        assert out[0].name == "OUTPUT_SSRC"
        assert out[0].head == 1
        # added is warn even for stream-critical names
        assert out[0].severity == "warn"

    def test_removed_stream_critical(self):
        pin  = [("OUTPUT_SSRC", 18, ""), ("FILTER_DROPS", 77, "")]
        head = [("FILTER_DROPS", 77, "")]
        out = _diff_enum("StatusType", pin, head)
        assert len(out) == 1
        assert out[0].kind == "removed"
        assert out[0].name == "OUTPUT_SSRC"
        assert out[0].pin == 18
        assert out[0].severity == "fail"

    def test_value_changed_stream_critical(self):
        pin  = [("OUTPUT_ENCODING", 107, "")]
        head = [("OUTPUT_ENCODING", 108, "")]
        out = _diff_enum("StatusType", pin, head)
        assert len(out) == 1
        assert out[0].kind == "value_changed"
        assert out[0].pin == 107
        assert out[0].head == 108
        assert out[0].severity == "fail"

    def test_value_changed_non_critical_is_warn(self):
        pin  = [("FILTER_DROPS", 77, "")]
        head = [("FILTER_DROPS", 78, "")]
        out = _diff_enum("StatusType", pin, head)
        assert out[0].severity == "warn"

    def test_encoding_value_shift_is_fail(self):
        pin  = [("F32LE", 4, ""), ("OPUS", 3, "")]
        head = [("F32LE", 5, ""), ("OPUS", 3, "")]
        out = _diff_enum("Encoding", pin, head)
        assert len(out) == 1
        assert out[0].name == "F32LE"
        assert out[0].severity == "fail"

    def test_window_change_is_warn(self):
        pin  = [("KAISER_WINDOW", 0, "")]
        head = [("KAISER_WINDOW", 1, "")]
        out = _diff_enum("WindowType", pin, head)
        assert out[0].severity == "warn"


# ---------------------------------------------------------------------------
# _aggregate_severity
# ---------------------------------------------------------------------------

class TestAggregateSeverity:
    def _delta(self, severities: list[str]) -> HeaderDelta:
        d = HeaderDelta(header="x", enum="y")
        for s in severities:
            d.changes.append(FieldChange("removed", "n", severity=s, reason=""))
        return d

    def test_all_pass(self):
        assert _aggregate_severity([]) == "pass"
        assert _aggregate_severity([self._delta([])]) == "pass"

    def test_warn_only(self):
        assert _aggregate_severity([self._delta(["warn", "warn"])]) == "warn"

    def test_fail_dominates(self):
        deltas = [self._delta(["warn"]), self._delta(["fail"])]
        assert _aggregate_severity(deltas) == "fail"

    def test_single_fail_in_otherwise_clean_set(self):
        deltas = [self._delta([]), self._delta(["fail"])]
        assert _aggregate_severity(deltas) == "fail"


# ---------------------------------------------------------------------------
# Sanity: every name in the stream-critical allowlist actually exists in
# the current types.py — guards against the allowlist drifting away from
# the schema.
# ---------------------------------------------------------------------------

def test_allowlist_names_exist_in_types():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ka9q.types import StatusType

    missing = [n for n in STREAM_CRITICAL_STATUS_TYPES
               if not hasattr(StatusType, n)]
    assert not missing, (
        f"stream-critical allowlist references unknown StatusType "
        f"name(s): {missing}.  Either the allowlist is stale or "
        f"types.py was regenerated without keeping these fields."
    )


# ---------------------------------------------------------------------------
# Read surface — which TLVs radiod actually EMITS
# ---------------------------------------------------------------------------
#
# ⛔ The enum diff above cannot see a direction change.  On 2026-09-21 Phil
# Karn proposed making PRESET write-only: radiod would still accept and
# execute it, but stop echoing it in status.  `enum status_type` keeps the
# name and the value, so every check above reports `pass` -- while
# ka9q-python's stream.py decides IQ-versus-real parsing from exactly that
# echoed field, and an IQ stream silently reads as audio.
#
# The checker equated "no header changed" with "contract intact" and returned
# early.  The emit calls live in src/radio_status.c, which it never opened.
# A TLV we depend on can therefore stop arriving without a single test
# noticing.  These pin the read surface instead.

from check_upstream_drift import (  # noqa: E402
    READ_SURFACE_FILE,
    _diff_read_surface,
    parse_read_surface,
)

# The real shape, verbatim from ka9q-radio src/radio_status.c @ cd44bbdd --
# note no space after the comma, and the mode-conditional nesting.
_REAL_ISH = '''
int encode_radio_status(struct frontend const *frontend,struct channel const *chan,uint8_t *packet,int len){
  uint8_t *bp = packet;
  *bp++ = STATUS;
  encode_int32(&bp,OUTPUT_SSRC,chan->output.rtp.ssrc);
  encode_int64(&bp,CMD_CNT,chan->commands);
  {
    int len = strlen(chan->preset);
    if(len > 0 && len < 256)
      encode_string(&bp,PRESET,chan->preset,len);
  }
  encode_int32(&bp,OUTPUT_SAMPRATE,chan->output.samprate);
  if(chan->demod_type == LINEAR_DEMOD){
    encode_float(&bp,DEMOD_SNR,chan->sig.snr);
  }
  encode_eol(&bp);
  return bp - packet;
}

// A DIFFERENT function must not contribute to the read surface.
int encode_something_else(uint8_t *packet){
  uint8_t *bp = packet;
  encode_int32(&bp,OUTPUT_TTL,9);
  return bp - packet;
}
'''


class TestParseReadSurface:

    def test_finds_the_tlvs_radiod_emits(self):
        got = parse_read_surface(_REAL_ISH)
        assert {"OUTPUT_SSRC", "CMD_CNT", "PRESET",
                "OUTPUT_SAMPRATE", "DEMOD_SNR"} <= got

    def test_ignores_emits_in_other_functions(self):
        """Only encode_radio_status defines what a client receives."""
        assert "OUTPUT_TTL" not in parse_read_surface(_REAL_ISH)

    def test_reads_the_real_header_style_without_spaces(self):
        """ka9q-radio writes `encode_string(&bp,PRESET,...)` with no space."""
        assert "PRESET" in parse_read_surface(
            "int encode_radio_status(void){encode_string(&bp,PRESET,x,1);}")

    def test_empty_source_yields_nothing_rather_than_raising(self):
        assert parse_read_surface("") == set()


class TestDiffReadSurface:

    def test_a_stream_critical_tlv_going_silent_is_a_fail(self):
        """THE case: PRESET still in the enum, no longer emitted."""
        changes = _diff_read_surface({"PRESET", "OUTPUT_SSRC"}, {"OUTPUT_SSRC"})
        assert len(changes) == 1
        assert changes[0].name == "PRESET"
        assert changes[0].severity == "fail"
        assert "emit" in changes[0].reason.lower() or \
               "echo" in changes[0].reason.lower()

    def test_a_non_critical_tlv_going_silent_only_warns(self):
        changes = _diff_read_surface({"CMD_CNT", "OUTPUT_SSRC"}, {"OUTPUT_SSRC"})
        assert changes[0].severity == "warn"

    def test_a_newly_emitted_tlv_is_an_opportunity_not_a_fault(self):
        changes = _diff_read_surface({"OUTPUT_SSRC"}, {"OUTPUT_SSRC", "GPS_TIME"})
        assert changes[0].severity == "warn"
        assert changes[0].kind == "newly_emitted"

    def test_no_change_reports_nothing(self):
        assert _diff_read_surface({"PRESET"}, {"PRESET"}) == []

    def test_the_watched_file_is_the_one_that_carries_the_emits(self):
        assert READ_SURFACE_FILE == "src/radio_status.c"


class TestParserFindsTheDefinitionNotThePrototype:
    """ka9q-radio forward-declares encode_radio_status ~670 lines above the
    definition.  Taking the first occurrence lands on the prototype, and the
    brace-match then walks an unrelated function -- yielding zero TLVs for
    BOTH revisions, so the diff sees nothing and the check passes on the very
    change it exists to catch.  A fixture without a prototype cannot see this;
    the real file has one."""

    _WITH_PROTOTYPE = '''
static unsigned long encode_radio_status(struct frontend const *frontend,chan_t *chan,uint8_t *packet,int len);

static int some_other_function(void){
  uint8_t *bp = p;
  encode_int32(&bp,OUTPUT_TTL,1);
  return 0;
}

static unsigned long encode_radio_status(struct frontend const *frontend,chan_t *chan,uint8_t *packet,int len){
  uint8_t *bp = packet;
  encode_string(&bp,PRESET,chan->preset,len);
  encode_int32(&bp,OUTPUT_SSRC,chan->output.rtp.ssrc);
  return bp - packet;
}
'''

    def test_it_reads_the_definition(self):
        got = parse_read_surface(self._WITH_PROTOTYPE)
        assert "PRESET" in got and "OUTPUT_SSRC" in got

    def test_it_does_not_walk_the_intervening_function(self):
        assert "OUTPUT_TTL" not in parse_read_surface(self._WITH_PROTOTYPE)

    def test_the_real_file_parses(self):
        """Guard against the fixture drifting from ka9q-radio's actual style."""
        real = Path("/home/mjh/hamsci/repos/ka9q-radio/src/radio_status.c")
        if not real.exists():
            pytest.skip("ka9q-radio checkout not present")
        got = parse_read_surface(real.read_text())
        assert len(got) > 50, f"only parsed {len(got)} TLVs from the real file"
        assert "PRESET" in got


# ---------------------------------------------------------------------------
# An enum that MOVES, and one we cannot read
# ---------------------------------------------------------------------------
#
# 2026-09-21: `enum demod_type` moved from src/radio.h to src/status.h
# upstream, values byte-identical.  The checker looked only where the map
# said, found nothing, and reported "could not parse enum demod_type" as a
# WARN -- the same severity as a cosmetic change to a field nobody reads.
#
# DemodType is in STREAM_CRITICAL_ENUMS.  "I could not check a stream-critical
# enum" is not the same statement as "a non-critical field changed", and
# collapsing them is how an unverified contract change ships.  On this
# occasion the values were unchanged and nothing broke; that was luck, not the
# check working.

from check_upstream_drift import (  # noqa: E402
    ENUM_SEARCH_PATHS,
    _classify_parse_failure,
    find_enum_text,
)


class TestEnumRelocation:
    """Look for an enum where it IS, not only where it used to be."""

    _RADIO_H = "enum demod_type {\n  LINEAR_DEMOD = 0,\n  FM_DEMOD,\n};\n"
    _STATUS_H = "enum status_type {\n  EOL = 0,\n};\n"

    def test_found_in_its_original_home(self):
        srcs = {"src/radio.h": self._RADIO_H, "src/status.h": self._STATUS_H}
        path, text = find_enum_text("demod_type", srcs)
        assert path == "src/radio.h" and "LINEAR_DEMOD" in text

    def test_found_after_it_moves(self):
        """The 2026-09-21 relocation: radio.h keeps everything but the enum."""
        srcs = {"src/radio.h": "struct channel { int x; };\n",
                "src/status.h": self._STATUS_H + self._RADIO_H}
        path, text = find_enum_text("demod_type", srcs)
        assert path == "src/status.h" and "LINEAR_DEMOD" in text

    def test_absent_everywhere_is_reported_as_absent(self):
        path, text = find_enum_text("demod_type", {"src/radio.h": "int x;"})
        assert path is None and text is None

    def test_the_search_covers_every_watched_header(self):
        """A new header must not silently fall outside the search."""
        assert set(ENUM_SEARCH_PATHS) >= {"src/status.h", "src/radio.h",
                                          "src/rtp.h", "src/window.h"}


class TestUnparseableStreamCriticalEnumFails:

    def test_a_stream_critical_enum_we_cannot_read_is_a_fail(self):
        sev, reason = _classify_parse_failure("DemodType")
        assert sev == "fail", reason
        assert "verif" in reason.lower() or "check" in reason.lower()

    def test_a_non_critical_enum_we_cannot_read_only_warns(self):
        sev, _ = _classify_parse_failure("WindowType")
        assert sev == "warn"

    def test_status_type_counts_as_stream_critical_here(self):
        """StatusType is not in STREAM_CRITICAL_ENUMS -- its criticality is
        per-field -- but failing to parse the TLV vocabulary ENTIRELY means
        no field can be checked at all."""
        sev, _ = _classify_parse_failure("StatusType")
        assert sev == "fail"


class TestSearchMatchesDeclarationsNotUsages:
    """A mention is not a declaration.

    The first version of find_enum_text matched `enum <name> ` anywhere, so a
    USAGE — `enum encoding e = parse_encoding(str);` — counted as the
    declaration.  Searching status.h first, it "found" Encoding there, handed
    parse_c_enum a header with no such enum, and the parse failure was then
    classified FAIL on a stream-critical enum.  A false alarm that blocks the
    pin is not a safe direction to fail in: it trains the next reader to
    override the check.
    """

    _DECL = "enum encoding {\n  NO_ENCODING = 0,\n  S16LE,\n};\n"
    _USAGE = "struct x { enum encoding e; };\nenum encoding f(char *s);\n"

    def test_a_usage_alone_is_not_found(self):
        path, text = find_enum_text("encoding", {"src/status.h": self._USAGE})
        assert path is None, f"matched a usage in {path}"

    def test_the_declaration_wins_over_an_earlier_usage(self):
        """status.h is searched before rtp.h; the declaration must still win."""
        srcs = {"src/status.h": self._USAGE, "src/rtp.h": self._DECL}
        path, text = find_enum_text("encoding", srcs)
        assert path == "src/rtp.h" and "NO_ENCODING" in text

    def test_brace_on_the_next_line_still_counts(self):
        srcs = {"src/rtp.h": "enum encoding\n{\n  NO_ENCODING = 0,\n};\n"}
        path, _ = find_enum_text("encoding", srcs)
        assert path == "src/rtp.h"

    def test_a_typedef_style_declaration_is_found(self):
        srcs = {"src/rtp.h": "typedef enum encoding {\n  NO_ENCODING = 0,\n} enc_t;\n"}
        path, _ = find_enum_text("encoding", srcs)
        assert path == "src/rtp.h"
