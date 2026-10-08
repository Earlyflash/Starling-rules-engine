# Running on a schedule (cron or systemd)

This tool is a one-shot script, not a daemon - `python -m starling_rules_engine`
polls once and exits (see `CLAUDE.md`). To get continuous coverage you run it
on a schedule, via cron or a systemd timer.

**On this machine, use the systemd timer** (see "Systemd timer" below) - it's
what's actually installed and running here. Cron is documented first below
because it's the simpler starting point and still a completely valid choice
elsewhere (e.g. a minimal box without systemd), but on a laptop like this one
systemd wins on a few points that matter in practice:

- `systemctl status starling-rules-engine.timer` tells you at a glance
  whether the last run succeeded and when the next one fires - no grepping a
  log file for that.
- `journalctl -u starling-rules-engine` gives structured, filterable logs
  for free - no `logrotate` config to write and maintain.
- **Catch-up after sleep.** A laptop that closes/sleeps means cron silently
  skips any run that would've fired while it was off, with no catch-up.
  `Persistent=true` on the systemd timer fires the missed run as soon as the
  laptop wakes instead - a reimbursement that lands overnight doesn't have
  to wait for the next scheduled slot after you wake it.

Worked examples below use placeholder paths/username - substitute your own
repo location and Linux user throughout:

- Repo: `/path/to/starling-rules-engine`
- Venv Python: `/path/to/starling-rules-engine/.venv/bin/python`
- User: `youruser`

## Prerequisites

Before putting this on a schedule, make sure you've already:

1. Run it by hand a few times and it works (`.venv/bin/python -m starling_rules_engine -v`).
2. Decided on `dry_run`. Leaving it `true` is entirely reasonable for a
   scheduled job - it'll log/alert what it *would* do, which is a good way
   to build confidence before ever setting `dry_run: false`. Don't put this
   on an unattended schedule with `dry_run: false` until you've read
   "Unattended real payments" below.
3. Set `STARLING_RULES_ALERT_WEBHOOK_URL` in `.env` if you want anything
   more than log-file visibility into what a scheduled run did. Without it,
   the only record is the log file you redirect output to below.

## The crontab entry

```
*/30 * * * * cd /path/to/starling-rules-engine && .venv/bin/python -m starling_rules_engine >> cron.log 2>&1
```

Edit with `crontab -e`. Breaking that down:

- `*/30 * * * *` - every 30 minutes. See "Choosing an interval" below for why
  30 minutes is a reasonable default rather than something to tune finely.
- `cd /path/to/starling-rules-engine &&` - **required**. `config.yaml`
  and `.env` are loaded from `--config`/`--env-file`, which default to
  `config.yaml`/`.env` *relative to the current directory* - cron runs jobs
  with an unspecified working directory (usually your home directory, not
  the repo), so without this `cd` the tool won't find its config at all and
  will exit with a config error every run.
- `.venv/bin/python` - **use the venv's Python explicitly, not `python3`**.
  Cron runs with a minimal environment - it does not read `.bashrc`/`.profile`,
  so a bare `python3` would use the system Python, which doesn't have
  `requests`/`PyYAML`/`python-dotenv`/`cryptography` installed, and the job
  would fail immediately. `.venv/bin/python` is a full path once combined
  with the `cd` above, so this works regardless of cron's own `PATH`.
- `-m starling_rules_engine` - no `-v`. Cron jobs run unattended - keep
  routine output to what you actually want in the log (one line per event,
  via `notifier.py`), not full debug HTTP tracing. Add `-v` back temporarily
  if you're debugging a scheduled run that isn't behaving as expected.
- `>> cron.log 2>&1` - append both stdout and stderr to `cron.log` in the
  repo directory. See "Logging" below for keeping this from growing forever.

### Choosing an interval

30 minutes is a reasonable default: frequent enough that a reimbursement
gets paid off the same day it lands, infrequent enough not to matter if
Starling's API has a brief blip (the next run picks up from
`last_poll_at` minus `poll_overlap_minutes`, nothing is lost - see `state.py`). `poll_lookback_minutes` in
`config.yaml` only matters for the very first run before `state.json`
exists; it's irrelevant to how often cron itself fires.

Don't go much tighter than 5-10 minutes - there's no benefit (reimbursements
don't need near-instant handling) and it multiplies API calls and log noise
for no real gain.

## Logging

`cron.log` grows forever unless something rotates it. Simplest option, a
`logrotate` config:

```
# /etc/logrotate.d/starling-rules-engine
/path/to/starling-rules-engine/cron.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
```

`logrotate` runs system-wide (see `/etc/cron.daily/logrotate` or its systemd
timer) so this needs no separate cron entry of its own.

If you'd rather not manage log rotation, redirect to `/dev/null` and rely
entirely on the alert webhook plus periodic `reconcile.py` checks instead -
but then a run that fails before it can even notify (e.g. a config or
network error at startup) leaves no trace at all, so this is only advisable
once you're confident the setup is stable.

## Unattended real payments

Once `dry_run: false` is in play, two things from real-world testing are
worth internalising before trusting this to run with nobody watching:

- **Starling may require phone approval** for a payment, entirely at their
  discretion (see README's "Limitations" section). If that happens on a
  cron run, the payment doesn't fail - it sits recorded as `pending_review`
  and gets re-checked (not re-submitted) on every subsequent run until you
  approve or decline it. Nothing is lost or duplicated, but it also won't
  complete until you check your phone. Don't assume "the log says nothing
  went wrong" means "the money moved."
- **Check `cron.log` (or your webhook) periodically, not just when something
  seems wrong.** A `pending_review` or `skipped_cap` outcome is exactly as
  important to notice as an `error` - none of them are silent failures, but
  none of them page you either. `python -m starling_rules_engine.reconcile`
  run by hand every so often (or as its own weekly cron entry logging to a
  separate file) is a good independent sanity check that what you expect to
  have been paid actually has been - see the main README.

## Testing before you trust it

Run the *exact* command from the crontab entry by hand first, including the
`cd` and the full venv path - don't just run `python -m starling_rules_engine`
from a shell where you've already `cd`'d into the repo and activated the
venv, since that hides exactly the two failure modes (wrong working
directory, wrong Python) cron is prone to:

```
cd /path/to/starling-rules-engine && .venv/bin/python -m starling_rules_engine >> cron.log 2>&1
cat cron.log
```

After adding the crontab entry, confirm cron actually picked it up and ran
it at the expected time:

```
crontab -l                                    # confirm the entry is saved
grep CRON /var/log/syslog | tail              # Debian/Ubuntu - or:
journalctl -u cron --since "1 hour ago"       # if cron runs under systemd
tail -f cron.log                              # watch the next run land
```

## Troubleshooting

- **"config error: config.yaml missing required key" or "file not found"
  on every cron run but not when run by hand** - almost always the missing
  `cd`. Cron's working directory is not the repo.
- **"No module named requests" (or similar)** - the crontab line is using
  `python3`/`python` instead of the venv's full path.
- **Nothing in `cron.log` at all, ever** - the job isn't firing. Check
  `crontab -l` is saved under the right user (cron user matters if you're
  not running this as `youruser`), and check `grep CRON /var/log/syslog`/
  `journalctl -u cron` for whether cron even attempted it.
- **Permission errors on `state.json` or `config.yaml`** - these need to be
  writable/readable by whichever user cron runs the job as. `crontab -e` as
  yourself edits *your* crontab, which runs as you - if you instead added
  the entry to `/etc/crontab` or `/etc/cron.d/`, it likely runs as `root`
  or whatever user that file specifies, which won't have access to files
  under `/home/youruser/`.
- **Payment signing fails on cron but works by hand** - check the signing
  private key file's permissions are readable by the cron user, and that
  `signing_private_key_path` in `config.yaml` isn't relying on `~`
  expansion breaking in some unexpected way (it's handled via `Path.expanduser()`
  in `config.py`, which needs `$HOME` to be set in the environment cron
  provides - normally fine for a user crontab, but worth checking if you've
  done anything unusual with cron's environment).

## Systemd timer

This is what's actually set up and running on this machine (as of
2026-08-23) - see "Which one should I use?" above for why.

User units (`~/.config/systemd/user/`) only run while you have an active
session *or* lingering is enabled - without it, closing your last session
(or the laptop being at the login screen with nobody logged in) stops the
timer firing, which defeats the point on a laptop. Enable it once:

```
loginctl enable-linger youruser
loginctl show-user youruser -p Linger    # should print Linger=yes
```

(Use system units under `/etc/systemd/system/` instead, with a `User=`
directive, if this ever needs to run on a headless server with nobody ever
logged in and you'd rather not rely on lingering.)

```ini
# ~/.config/systemd/user/starling-rules-engine.service
[Unit]
Description=Starling rules engine poll
StartLimitIntervalSec=1800
StartLimitBurst=5

[Service]
Type=oneshot
WorkingDirectory=/path/to/starling-rules-engine
ExecStart=/path/to/starling-rules-engine/.venv/bin/python -m starling_rules_engine
Restart=on-failure
RestartSec=300
```

`Restart=on-failure` + `RestartSec=300` covers the most common wake-from-sleep
failure: `Persistent=true` fires the missed run the moment systemd notices
it's overdue, which on a laptop is often before Wi-Fi has reassociated -
`starling_client.py`'s request then fails with a connection error, the run
exits non-zero, and without this it would just wait for tomorrow's slot.
This retries up to `StartLimitBurst` times, 5 minutes apart, before giving
up and leaving the unit in a failed state (`systemctl --user status` /
`journalctl` will show it). Nothing is at risk either way - `last_poll_at`
in `state.json` only advances after a run fully succeeds (see `engine.py`),
so a failed attempt just delays, never double-processes. `StartLimitIntervalSec=1800`
widens systemd's default 10-second retry-counting window so all 5 attempts
(20 minutes span) actually get to run rather than tripping the default burst
limit early and failing permanently.

```ini
# ~/.config/systemd/user/starling-rules-engine.timer
[Unit]
Description=Run starling-rules-engine daily at 19:00

[Timer]
OnCalendar=*-*-* 19:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Currently set to once a day at 19:00, deliberately, rather than the
`*/30`-style interval mentioned earlier - while getting comfortable with
real payments, it's worth being around at run time to see the outcome
and handle any phone approval prompt (see "Unattended real payments"
above) rather than finding out later. Once that's no longer a concern,
switch back to a tighter interval, e.g. `OnCalendar=*:0/30` for every 30
minutes, matching the crontab example earlier - then `systemctl --user
daemon-reload && systemctl --user restart starling-rules-engine.timer`
to pick it up.

```
systemctl --user daemon-reload
systemctl --user enable --now starling-rules-engine.timer
```

`WorkingDirectory=` in the service unit replaces the `cd &&` trick from the
crontab line - same underlying reason it's needed (config paths are
relative to the working directory). `Persistent=true` on the timer means a
missed run (e.g. laptop was asleep) fires as soon as it's back up, rather
than waiting for the next scheduled slot - reasonable here since a
slightly late poll costs nothing.

### Checking on it

```
systemctl --user status starling-rules-engine.timer     # is it scheduled? when's next?
systemctl --user list-timers starling-rules-engine.timer
journalctl --user -u starling-rules-engine.service -f    # follow live
journalctl --user -u starling-rules-engine.service -n 50 --no-pager  # last 50 lines
```

To trigger a run right now without waiting for the schedule (useful for
testing a config change):

```
systemctl --user start starling-rules-engine.service
```
