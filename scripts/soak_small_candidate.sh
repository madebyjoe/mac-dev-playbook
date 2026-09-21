#!/usr/bin/env bash
#
# soak_small_candidate.sh — evidence for G-MDP4-3 (MDP-4, T-MDP4-10).
#
# Runs a candidate `small` model under REALISTIC CONCURRENT LOAD and reports
# whether the node survives it. The question this answers is not "is the model
# good" -- it is "does putting this model on the pinned pipeline break the two
# things that already live there". On the M1 Pro that failure mode is known and
# specific: an over-subscribed Metal allocator returns `Compute error.` 500s
# from `embed`, intermittently, only when something else was loaded first. A
# model that benchmarks beautifully in isolation can still do that.
#
# So all three pipeline consumers run AT THE SAME TIME for the whole duration:
#   1. chat completions against the candidate, at ~500 and ~4000 input tokens
#   2. /v1/embeddings against the `embed` model
#   3. transcription against the whisper-server endpoint
# and the machine is sampled every 15s for memory pressure, swap and residency.
#
# SR2: every prompt is synthetic filler generated here, and the audio is
# generated on the spot with `say` -- there is no real user content anywhere in
# this script, by construction.
#
# NOTHING HERE IS DESTRUCTIVE. It makes requests and writes one report file. It
# never pulls, never evicts, and never changes an alias -- ratifying a candidate
# is a human act (F-MDP4-7); see runbooks/model-management.md.
#
# Usage:
#   scripts/soak_small_candidate.sh <candidate-tag> [duration-seconds] [ollama-base-url]
#
# Env overrides:
#   EMBED_MODEL      model for the embeddings load   (default qwen3-embedding:4b)
#   TRANSCRIBE_URL   whisper-server OpenAI endpoint  (default http://127.0.0.1:8082/v1/audio/transcriptions)
#   TRANSCRIBE_MODEL model name sent to it           (default whisper-large-v3-turbo)
#   OUT_DIR          where the report lands          (default .)

set -euo pipefail

CANDIDATE="${1:-}"
DURATION="${2:-1200}"                       # 20 minutes
BASE_URL="${3:-http://127.0.0.1:11434}"
EMBED_MODEL="${EMBED_MODEL:-qwen3-embedding:4b}"
TRANSCRIBE_URL="${TRANSCRIBE_URL:-http://127.0.0.1:8082/v1/audio/transcriptions}"
TRANSCRIBE_MODEL="${TRANSCRIBE_MODEL:-whisper-large-v3-turbo}"
OUT_DIR="${OUT_DIR:-.}"
SAMPLE_INTERVAL=15

if [ -z "$CANDIDATE" ]; then
  cat >&2 <<'USAGE'
usage: soak_small_candidate.sh <candidate-tag> [duration-seconds] [ollama-base-url]

  <candidate-tag>     e.g. qwen3.6:35b-mlx  (must already be pulled)
  [duration-seconds]  default 1200 (20 min)
  [ollama-base-url]   default http://127.0.0.1:11434
USAGE
  exit 2
fi

for tool in curl python3 say; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing required tool: $tool" >&2; exit 2; }
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --------------------------------------------------------------------------- #
# Synthetic prompts. Deterministic filler, sized by character count -- roughly
# 4 characters per token for English, so 2000 chars ~ 500 tokens and 16000 chars
# ~ 4000 tokens. Approximate ON PURPOSE: the point is two clearly different
# context sizes, because MLX does a full prefill before the first token and TTFT
# therefore grows with input length (manifest note 6).
# --------------------------------------------------------------------------- #
make_filler() {
  python3 -c '
import sys
n = int(sys.argv[1])
unit = ("The quarterly maintenance log records routine checks of the ventilation "
        "system, the backup generator, and the water filtration loop. ")
sys.stdout.write((unit * (n // len(unit) + 1))[:n])
' "$1"
}

PROMPT_SMALL="$(make_filler 2000)"
PROMPT_LARGE="$(make_filler 16000)"

# --------------------------------------------------------------------------- #
# Synthetic audio, generated here so no real recording is ever involved (SR2).
# --------------------------------------------------------------------------- #
AUDIO="$WORK/probe.wav"
say -o "$WORK/probe.aiff" \
  "This is a synthetic test clip generated for a soak run. It contains no real content." \
  >/dev/null 2>&1 || true
if [ -f "$WORK/probe.aiff" ] && command -v ffmpeg >/dev/null 2>&1; then
  ffmpeg -hide_banner -loglevel error -i "$WORK/probe.aiff" \
    -ar 16000 -ac 1 -y "$AUDIO" >/dev/null 2>&1 || true
fi
[ -f "$AUDIO" ] || AUDIO=""

# --------------------------------------------------------------------------- #
# Workers. Each appends one line per request to its own file, so nothing has to
# be shared or locked between the concurrent loops.
# --------------------------------------------------------------------------- #
CHAT_LOG="$WORK/chat.tsv"        # size<TAB>http<TAB>ttft_s<TAB>total_s<TAB>chunks
EMBED_LOG="$WORK/embed.tsv"      # http
TRANS_LOG="$WORK/transcribe.tsv" # http
SAMPLE_LOG="$WORK/samples.tsv"   # epoch<TAB>pressure<TAB>swap_mb<TAB>resident<TAB>free_pct
: > "$CHAT_LOG"; : > "$EMBED_LOG"; : > "$TRANS_LOG"; : > "$SAMPLE_LOG"

END_AT=$(( $(date +%s) + DURATION ))

chat_once() {
  local label="$1" prompt="$2" body resp http ttft total chunks
  body=$(python3 -c '
import json,sys
print(json.dumps(dict(
    model=sys.argv[1], stream=True, max_tokens=128,
    messages=[dict(role="user", content=sys.argv[2] + "\n\nSummarise the above in one sentence.")],
)))' "$CANDIDATE" "$prompt")

  # `-N` disables curl buffering so time_starttransfer is a real time-to-first-
  # token and not an artefact of curl holding the stream. TTFT is measured as
  # time to first BYTE of the response body, which for a streamed completion is
  # the first token -- stated plainly because it is an approximation.
  resp=$(curl -sS -N -m 300 \
      -o "$WORK/chat.out" \
      -w '%{http_code}\t%{time_starttransfer}\t%{time_total}' \
      -H 'Content-Type: application/json' \
      -d "$body" "$BASE_URL/v1/chat/completions" 2>/dev/null) || resp=$'000\t0\t0'
  http=$(printf '%s' "$resp" | cut -f1)
  ttft=$(printf '%s' "$resp" | cut -f2)
  total=$(printf '%s' "$resp" | cut -f3)
  chunks=$(grep -c '^data: ' "$WORK/chat.out" 2>/dev/null || true)
  [ -n "$chunks" ] || chunks=0
  printf '%s\t%s\t%s\t%s\t%s\n' "$label" "$http" "$ttft" "$total" "$chunks" >> "$CHAT_LOG"
  # A real request self-paces (it takes seconds). A REFUSED one returns
  # instantly, which would otherwise spin this loop thousands of times against a
  # down backend and bury the real signal under noise.
  [ "$http" = "200" ] || sleep 2
}

chat_worker() {
  while [ "$(date +%s)" -lt "$END_AT" ]; do
    chat_once 500 "$PROMPT_SMALL"
    [ "$(date +%s)" -lt "$END_AT" ] || break
    chat_once 4000 "$PROMPT_LARGE"
  done
}

embed_worker() {
  local body http
  body=$(python3 -c '
import json,sys
print(json.dumps(dict(model=sys.argv[1], input=[sys.argv[2][:1200]])))' \
    "$EMBED_MODEL" "$PROMPT_SMALL")
  while [ "$(date +%s)" -lt "$END_AT" ]; do
    http=$(curl -sS -m 120 -o /dev/null -w '%{http_code}' \
        -H 'Content-Type: application/json' \
        -d "$body" "$BASE_URL/v1/embeddings" 2>/dev/null) || http=000
    echo "$http" >> "$EMBED_LOG"
    sleep 5
  done
}

transcribe_worker() {
  local http
  if [ -z "$AUDIO" ]; then
    echo "skipped" >> "$TRANS_LOG"
    return 0
  fi
  while [ "$(date +%s)" -lt "$END_AT" ]; do
    http=$(curl -sS -m 180 -o /dev/null -w '%{http_code}' \
        -F "file=@$AUDIO" -F "model=$TRANSCRIBE_MODEL" \
        "$TRANSCRIBE_URL" 2>/dev/null) || http=000
    echo "$http" >> "$TRANS_LOG"
    sleep 20
  done
}

sampler() {
  local pressure swap resident now
  while [ "$(date +%s)" -lt "$END_AT" ]; do
    now=$(date +%s)
    # The LEVEL comes from the kernel, not from `memory_pressure` -- on current
    # macOS that tool prints statistics and a free percentage but no level word
    # at all, so scraping it for "normal|warn|critical" silently yields nothing
    # and every sample records "unknown". kern.memorystatus_vm_pressure_level is
    # the value the OS itself dispatches on: 1 normal, 2 warn, 4 critical.
    case "$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null || echo)" in
      1) pressure=normal ;;
      2) pressure=warn ;;
      4) pressure=critical ;;
      *) pressure=unknown ;;
    esac
    # Free percentage is recorded alongside it for context, not for pass/fail.
    freepct=$(memory_pressure 2>/dev/null \
      | sed -n 's/.*free percentage: \([0-9]*\)%.*/\1/p' | tail -1 || true)
    [ -n "$freepct" ] || freepct=""
    # vm.swapusage reports like: total = 2048.00M  used = 123.45M  free = ...
    swap=$(sysctl -n vm.swapusage 2>/dev/null \
      | sed -n 's/.*used = \([0-9.]*\)M.*/\1/p' || true)
    [ -n "$swap" ] || swap=0
    resident=$(curl -sS -m 10 "$BASE_URL/api/ps" 2>/dev/null \
      | python3 -c '
import json,sys
try:
    doc = json.load(sys.stdin) or dict()
except Exception:
    print(""); raise SystemExit
print(",".join(m.get("name","") for m in (doc.get("models") or [])))' || true)
    printf '%s\t%s\t%s\t%s\t%s\n' "$now" "$pressure" "$swap" "$resident" "$freepct" >> "$SAMPLE_LOG"
    sleep "$SAMPLE_INTERVAL"
  done
}

echo "soak: candidate=$CANDIDATE duration=${DURATION}s base=$BASE_URL"
if [ -n "$AUDIO" ]; then
  echo "soak: embed=$EMBED_MODEL transcribe=enabled ($TRANSCRIBE_URL)"
else
  echo "soak: embed=$EMBED_MODEL transcribe=DISABLED (could not generate audio: need say + ffmpeg)"
fi
echo "soak: sampling every ${SAMPLE_INTERVAL}s; this is read-only, nothing is pulled or evicted"

chat_worker &       CHAT_PID=$!
embed_worker &      EMBED_PID=$!
transcribe_worker & TRANS_PID=$!
sampler &           SAMPLE_PID=$!

# Workers exit on their own at END_AT; wait for each and ignore their status so
# one failing loop cannot abort the whole run before the report is written.
for pid in "$CHAT_PID" "$EMBED_PID" "$TRANS_PID" "$SAMPLE_PID"; do
  wait "$pid" 2>/dev/null || true
done

# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
REPORT="$OUT_DIR/soak-$(printf '%s' "$CANDIDATE" | tr '/:' '--')-$(date +%Y%m%d).md"

python3 - "$CHAT_LOG" "$EMBED_LOG" "$TRANS_LOG" "$SAMPLE_LOG" \
         "$CANDIDATE" "$EMBED_MODEL" "$DURATION" "$BASE_URL" > "$REPORT" <<'PY'
import sys, datetime

chat_p, embed_p, trans_p, sample_p, candidate, embed_model, duration, base = sys.argv[1:9]


def rows(path, n):
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == n:
                    out.append(parts)
    except OSError:
        pass
    return out


def pct(values, q):
    if not values:
        return None
    vals = sorted(values)
    # Nearest-rank percentile: no interpolation, so a reported p95 is always a
    # request that actually happened.
    k = max(0, min(len(vals) - 1, int(round(q * (len(vals) - 1)))))
    return vals[k]


def fmt(v, unit="s"):
    return "n/a" if v is None else "%.2f%s" % (v, unit)


chat = rows(chat_p, 5)
samples = rows(sample_p, 5)
embed = [r[0] for r in rows(embed_p, 1)]
trans = [r[0] for r in rows(trans_p, 1)]

L = []
A = L.append
A("# Soak report — `%s`" % candidate)
A("")
A("Generated %s by `scripts/soak_small_candidate.sh`. Evidence for **G-MDP4-3**."
  % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
A("")
A("| | |")
A("|---|---|")
A("| Candidate | `%s` |" % candidate)
A("| Embed model | `%s` |" % embed_model)
A("| Duration | %s s |" % duration)
A("| Ollama base | %s |" % base)
A("| Chat requests | %d |" % len(chat))
A("| Embed requests | %d |" % len(embed))
A("| Transcribe requests | %d |" % len([t for t in trans if t != "skipped"]))
A("| Samples | %d |" % len(samples))
A("")

A("## Latency by input size")
A("")
A("TTFT is time to the first response byte of a streamed completion. Decode "
  "rate is (chunks - 1) / (total - TTFT), where one SSE chunk is one token — an "
  "approximation, stated so nobody reads it as instrumented truth.")
A("")
A("| Input | Requests | non-200 | TTFT p50 | TTFT p95 | decode tok/s (median) |")
A("|---|---|---|---|---|---|")
for size in ("500", "4000"):
    got = [r for r in chat if r[0] == size]
    bad = len([r for r in got if r[1] != "200"])
    ttfts, rates = [], []
    for _s, code, ttft, total, chunks in got:
        if code != "200":
            continue
        try:
            t0, t1, n = float(ttft), float(total), int(chunks)
        except ValueError:
            continue
        ttfts.append(t0)
        if n > 1 and t1 > t0:
            rates.append((n - 1) / (t1 - t0))
    A("| ~%s tok | %d | %d | %s | %s | %s |"
      % (size, len(got), bad, fmt(pct(ttfts, .50)), fmt(pct(ttfts, .95)),
         fmt(pct(rates, .50), "")))
A("")

A("## Endpoint health")
A("")
A("| Endpoint | Requests | non-200 | codes seen |")
A("|---|---|---|---|")


def codes(seq):
    seen = {}
    for c in seq:
        seen[c] = seen.get(c, 0) + 1
    return ", ".join("%s×%d" % (k, v) for k, v in sorted(seen.items())) or "—"


chat_codes = [r[1] for r in chat]
A("| chat (`%s`) | %d | %d | %s |"
  % (candidate, len(chat_codes), len([c for c in chat_codes if c != "200"]), codes(chat_codes)))
A("| embeddings (`%s`) | %d | %d | %s |"
  % (embed_model, len(embed), len([c for c in embed if c != "200"]), codes(embed)))
real_trans = [t for t in trans if t != "skipped"]
A("| transcribe | %d | %d | %s |"
  % (len(real_trans), len([c for c in real_trans if c != "200"]), codes(real_trans)))
A("")

swaps = []
for r in samples:
    try:
        swaps.append(float(r[2]))
    except ValueError:
        pass
swap_delta = (max(swaps) - min(swaps)) if swaps else None
levels = [r[1].lower() for r in samples]
order = {"normal": 0, "warn": 1, "warning": 1, "critical": 2, "unknown": -1}
worst = max(levels, key=lambda x: order.get(x, -1)) if levels else "n/a"

resident_end = samples[-1][3] if samples else ""
resident_names = [n for n in resident_end.split(",") if n]

A("## Machine under load")
A("")
A("| | |")
A("|---|---|")
A("| Swap used, min → max | %s → %s MB |"
  % (fmt(min(swaps) if swaps else None, ""), fmt(max(swaps) if swaps else None, "")))
A("| **Max swap delta** | %s MB |" % fmt(swap_delta, ""))
A("| **Worst memory pressure** | %s |" % worst)
free_pcts = [int(r[4]) for r in samples if r[4].isdigit()]
A("| Memory free %%, min → max | %s → %s |"
  % (min(free_pcts) if free_pcts else "n/a", max(free_pcts) if free_pcts else "n/a"))
A("| Resident at end (`/api/ps`) | %s |" % (", ".join("`%s`" % n for n in resident_names) or "—"))
A("")

# --- pass/fail ------------------------------------------------------------- #
checks = [
    ("zero non-200 on embeddings",
     len(embed) > 0 and all(c == "200" for c in embed)),
    ("zero non-200 on transcribe",
     len(real_trans) > 0 and all(c == "200" for c in real_trans)),
    ("swap delta < 1024 MB",
     swap_delta is not None and swap_delta < 1024),
    ("memory pressure never critical",
     "critical" not in levels),
    ("candidate still resident at end",
     any(candidate.split(":")[0] in n for n in resident_names)),
    ("embed model still resident at end",
     any(embed_model.split(":")[0] in n for n in resident_names)),
]
A("## Verdict")
A("")
A("| Check | Result |")
A("|---|---|")
for label, ok in checks:
    A("| %s | %s |" % (label, "PASS" if ok else "**FAIL**"))
A("")
overall = all(ok for _l, ok in checks)
A("**%s**" % ("PASS" if overall else "FAIL"))
A("")
A("TTFT is deliberately NOT part of pass/fail — the acceptable budget is a human "
  "call against G19 voice latency, made by reading the table above.")
A("")
A("Record which backend actually served this (`mlx` vs GGML) before acting on it "
  "(N4): the M1 Pro sits exactly at MLX's documented 32 GB floor and falls back "
  "to GGML silently.")
A("")
A("```sh")
A("grep -iE 'mlx|ggml|metal' ~/Library/Logs/ollama/ollama.log | tail -20")
A("```")
A("")
A("A PASS is evidence, not a decision. Moving `serves_alias: small` is a human "
  "act — see `runbooks/model-management.md`.")
print("\n".join(L))
PY

echo
echo "soak: report written to $REPORT"
grep -E '^\*\*(PASS|FAIL)\*\*' "$REPORT" || true
