#!/bin/zsh
# Checks that test_h2d_logger.py notices deliberate mistakes: each line below breaks one thing in a temporary copy of
# h2d_logger.py and the suite must then FAIL. Run next to the two files:  zsh mutation_check.sh
PY=${PY:-python3}
SRC="${0:A:h}"
caught=0; total=0
check() {   # name, sed expression
  total=$((total + 1))
  d=$(mktemp -d /tmp/h2dmut.XXXXXX)
  cp "$SRC/test_h2d_logger.py" "$d/"
  sed -E "$2" "$SRC/h2d_logger.py" > "$d/h2d_logger.py"
  if cmp -s "$SRC/h2d_logger.py" "$d/h2d_logger.py"; then echo "NOT APPLIED: $1"; rm -rf "$d"; return; fi
  if (cd "$d" && $PY test_h2d_logger.py >/dev/null 2>&1); then echo "MISSED:  $1"; else echo "caught:  $1"; caught=$((caught + 1)); fi
  rm -rf "$d"
}
check "target temperature decoded with the wrong shift" 's/\(v >> 16\) & 0xFFFF/(v >> 8) \& 0xFFFF/'
check "retention back to 7 days"                         's/^KEEP_DAYS = 31/KEEP_DAYS = 7/'
check "log server serves any file name"                  's/if name and not re\.fullmatch\(r"status-\\d\{4\}-\\d\{2\}-\\d\{2\}\\\.csv", name\):/if False:/'
check "stale connection never detected"                  's/if now - last_rx > STALE:/if False:/'
check "partial updates replace instead of merge"         's/^            merge\(into\[k\], v\)/            into[k] = v/'
check "access code printed when refused"                 's/4 or 5 = wrong access code"/4 or 5 = wrong access code " + cfg["access_code"]/'
check "QoS 1 packet id not skipped"                      's/\(2 if \(head >> 1\) & 3 else 0\)/0/'
check "no pushall request sent"                          's/"command":"pushall"/"command":"nothing"/'
check "cleanup deletes undated files too"                's/if m and m\.group\(1\) < cutoff:/if (not m) or m.group(1) < cutoff:/'
check "fan never switched off at the end of a job"      's/^        if self\.fan_job is not None:/        if False:/'
check "PLA jobs switch the fan on too"                    's/re\.compile\(r"\(ABS\|ASA\|PETG\)/re.compile(r"(ABS|ASA|PETG|PLA)/'
check "a partial report switches the fan off mid-print"   's/if job is None or job == self\.fan_job:/if job == self.fan_job:/'
check "the fan's job not kept across a restart"           's/json\.dump\(\{"job": job,/json.dump({"job": None,/'
check "filament slots numbered the old way"               's/int\(unit\) << 8 \| int\(tray\)/int(unit) * 4 + int(tray)/'
check "a failed fan check holds up the next print"        's/self\.next_check = now \+ \(FAN_CHECK if ok else 3600\)/self.next_check = now + (FAN_CHECK if ok else 3600); self.next_try = now + (0 if ok else 600)/'
check "a failed fan command is never retried"             's/self\.next_try, self\.delay = now \+ self\.delay,/self.fan_job, self.next_try, self.delay = job, now + self.delay,/'
check "the fan comes on during the warm-up"              's/^                if ready:/                if True:/'
check "a chamber target of 0 counts as reached"           's/if target and now >= target:/if now >= target:/'
check "printing taken from the stage alone"               's/if get\(state, "print\.gcode_state"\) == "RUNNING" and str/if str/'
echo "$caught of $total deliberate mistakes caught"
