# CM Salesforce Sync

Keeps every **case manager's Convoso campaign** matching **Salesforce**, every weekday, by itself.
It replaces the weekly Lead Track file that back office used to drop in `#cm-uploads`.

- **Live since:** 2026-10-27. First sync approved by Pranay at 5:17 PM and done at 5:22 PM: **110 added, 134 moved,
  163 removed, 1 failed** (Convoso rejected an invalid SF phone), out of 408 planned. Result and Asana report in `#cm-uploads`.
- **Built by:** manager savva 2, Savva team. Dry-run build T-0012 (worker 1), QA T-0013 (worker 4), one-lead live test.
- **Owner:** Pranay. **Data owner:** Eli (head of CMs), who keeps case managers right in Salesforce.

---

## 1. Why it exists
Eli now assigns case managers **directly in Salesforce**, so the Lead Track file went stale (the last one was 10/16; none came on 10/23).
On 10/27 the CM campaigns had 116 clients with the wrong case manager, 54+ cancelled clients still being dialed, and
Kalyssa's new clients missing. Case managers get reassigned about 130 times a week, so a weekly file is always out of date.

## 2. The rules (decided by Pranay, 2026-10-27)
| Rule | Detail |
|---|---|
| **Who is a client** | A client belongs to their case manager **from Contract Signed until Cancelled**, whatever else applies (NSF, EFT, Fees Done). SOQL: `Case_Manager__c != null AND StageName IN ('Contract Signed','Enrolled','Fees Done','NSF')` |
| **Not clients** | Build Plan, Contract Sent, Approved, UW, Docs (the CM is pre-filled there, but they're still in sales). **Cancelled = no longer with DCG.** |
| **Case manager changes** | The **same Convoso lead moves** to the new CM's campaign, so call history is kept. One client = one lead, in exactly one campaign |
| **Cancelled** | The lead comes off dialing (moved to an inactive list in its campaign) |
| **DNC** | **Always respected.** A DNC'd client is never added, and nothing is ever removed from DNC |
| **Convoso setup** | Unchanged: one campaign per CM, and its current active list. Nothing new is created |
| **Phone numbers** | **Salesforce is the source of truth.** Taken from SF `Mobile`, else `Phone`. When SF has a different number, the same Convoso lead gets the SF number (history kept). Placeholders are never used: numbers like 949-xxx-1111 (2 or fewer distinct digits) any number shared by 3+ clients (office line), and **any number of a Salesforce user (staff / case manager)**: we only ever dial the client. Without a real SF number, Convoso's number is left as is |
| **Duplicate leads** | One client = one lead. If Convoso has 2+ leads for a client, the one with the SF number (else the oldest, in the right campaign) is kept; the others come off dialing |
| **Shared number** | Two clients with the same number in SF are both clients, so each goes to their own CM |
| **Weekends** | Skipped |

## 3. What happens every weekday
| Time | What | In `#cm-uploads` |
|---|---|---|
| **4:30 PM** | Preview, reads only | "🔎 CM Sync Preview": per CM, plus the list in the thread |
| **5:00 PM** | Builds today's plan from live SF + Convoso | "⏸️ CM Sync — ready, nothing changed yet" with **✅ Sync now** / **❌ Reject** buttons |
| until **6:00 PM** | Anyone in the channel can press **Sync now** (runs now) or **Reject** (no sync today) | The buttons get replaced by "Sync started by @x" / "Rejected by @x" |
| **6:00 PM** | **If nobody rejected, it syncs by itself** | "⏰ Nobody rejected the plan, so CM Sync is syncing it now." |
| while syncing | New clients first, then moves, then removals. A **🛑 Stop** button stops it after the current lead | "▶️ CM Sync applying N changes…" |
| when done | Result per case manager, plus an Asana report under the CM epic | "✅ CM Sync done — Eli Paredes — 4 new, 5 moved in, 1 moved out, 1 removed …" |

- **PC asleep or off at 5 PM:** it runs when the PC is back (until 7 PM) and still waits at least 30 min for a Reject.
- **Nothing to change:** "✅ everything already matches Salesforce".

### Reading the summary
| Line | Meaning |
|---|---|
| `new` | Client just reached Contract Signed (or has no lead yet), added to their CM's campaign |
| `moved in` / `moved out` | Eli reassigned the client. The same lead moved between the two CMs' campaigns |
| `removed` | Cancelled client (or a duplicate copy of a client) taken off dialing |
| `🔒 on DNC: kept off` | Client is on that campaign's DNC. **Kept off on purpose** |
| `phone updated` | SF had a different number; the lead now has the SF number |
| `no phone in Salesforce` | Add a phone in SF; the next run picks it up |
| `case manager has no campaign` | A new CM in SF without a Convoso campaign. Add them to `cm_map.json` |

## 4. Slack commands (type in `#cm-uploads`; anyone can use them)
| Command | Does |
|---|---|
| `sync help` | Lists the commands |
| `sync status` | Paused?, next run, what's waiting, last run |
| `sync preview` | Shows what a sync would change right now. Changes nothing |
| `sync now` | Runs a sync now and applies it straight away |
| `sync proceed` / `sync cancel` | Same as the Sync now / Reject buttons |
| **`sync stop`** | **Emergency:** stops a running sync after the current lead AND pauses the schedule |
| `sync hold` / `sync resume` | Pauses / restarts the daily schedule |
| `sync history` | The last 10 runs |
| `sync client <file#>` | Everything the sync did to that client (or use the last 4 digits of the phone) |

All commands start with `sync`, so they never trigger the old CM upload bot (`proceed`, `activate`, `dnc` …).

## 5. When something goes wrong
| Problem | What happens | Where you see it |
|---|---|---|
| SF or Convoso unreachable / login fails | Stops before changing anything; retries every 20 min until 7 PM (3 tries) | `#cm-uploads` "❌ CM Sync failed … nothing was changed" |
| Incomplete read, or counts drop under 60% of the last run | Plan refused (this stops wrong mass removals) | `#cm-uploads` "sanity check failed" |
| One lead fails | That lead is skipped, the rest continue. A write is never retried, so no duplicates | Summary "· N failed", names in the thread |
| Someone spots a problem | 🛑 Stop / `sync stop` | `#cm-uploads` |
| Bot crashes | The restart loop brings it back in 15 s; Windows Startup brings it back after a reboot | If a plan was waiting, it is rebuilt and reposted |
| **Bot down, stuck, or no 5 PM post** | **Watchdog** (savva pinger, every minute): restarts it (max 3 an hour); alerts once if stuck for 10 min or if there's no 5 PM plan by 5:20 PM | **Slack DM to Pranay** via Agent Savva |
| Buttons stop working | Socket Mode reconnects every 30 s; typed commands always work | — |

Every applied change is recorded in `changes.csv` (time, client, action, CM from → to, lead id, result).

## 6. Files (`Convoso\cm_sync\`)
| File | What |
|---|---|
| `sync_service.py` | The service: schedule, Slack messages and buttons, commands, tracking, Asana report |
| `cm_sf_sync.py` | SF read (read-only), Convoso read, the plan (`build_plan`), `apply_rows` (**the only code that writes**) |
| `cm_map.json` | SF case manager **user id** → Convoso campaign. `retired` = CM gone; `ignore` = test accounts (Adarsha) |
| `.env` | `CM_SLACK_APP_TOKEN` (Socket Mode, `xapp-`). Git-ignored. The bot token and channel come from `..\cm\.env`, SF from the vault, Convoso/Asana from `..\.env` |
| `APPLY_VERIFIED` | Created after the one-lead live test passed. **Delete it to lock all live changes instantly** |
| `state.json` | Paused?, waiting plan, last runs, last counts (sanity check), processed Slack messages |
| `changes.csv` | Every applied change and its result (phones masked) |
| `logs\service.log` | Service log. `logs\plan_<date_time>.csv` = each plan |
| `start_sync.bat` / `start_sync.vbs` | Restart loop / hidden launcher. A copy of the .vbs is in Startup as `cm_sf_sync.vbs` |
| `test_plan.py` | Offline tests (`python -I test_plan.py`, must print ALL PASSED) |
| `one_lead_test.py` | The one-time live test from go-live (1 move + 1 phone) |

## 7. Case managers (`cm_map.json`, checked against SF and Convoso on 2026-10-27)
| Case manager | Convoso campaign | Note |
|---|---|---|
| Alondra Alva | 4135 | |
| Alquinton Smith | 4999 | |
| Eli Paredes | 5003 | |
| Joshua Cardenas | 6269 | |
| Maximiliano Muller | 4991 | |
| Patricia Tercier | 5007 | |
| Sammaurae Barr | 4993 | |
| Wanicka Cassagnol | 4995 | |
| Kalyssa Acosta | 4997 | Its active list is still the old "Asmeri list 07-24-26" (14807) |
| Ebonee Wright | 6871 | Two SF users (old inactive one + new 10/01); both map here |
| Stefan Rivera | 6181 | **0 clients in SF since 10/27.** Retire if he has left |
| Darrell Fair | 4985 | Retired (inactive in SF) |
| Adarsha Yemparala | — | Test account, ignored |

Daniela Ortiz (4989) has a Convoso campaign but no clients in SF, so it's not in the map.

## 8. How to …
- **Restart:** stop the python PID running `sync_service.py` (**never kill by name**). The .bat restarts it in 15 s.
- **Stop completely:** stop the `start_sync.bat` cmd PID, then the python PID. (The watchdog will restart it. Pause it with `sync hold` instead.)
- **Lock all live changes:** delete `APPLY_VERIFIED`. Plans still post, and nothing is written.
- **Add a new case manager:** create their Convoso campaign, then add `"<SF user id>": {"name": "...", "campaign_id": "..."}` to `cm_map.json`, then restart.
- **Retire a case manager:** add `"retired": true` to their entry; their leads move to the new CMs or come off dialing.
- **Turn phone updates off:** set `UPDATE_PHONES = False` in `cm_sf_sync.py`, then restart (differences are then only listed).
- **Check health:** `python -I C:\Users\PranaySavva\savva-team\bus\savva.py fleet` → `cm sync UP`.

## 9. Lessons and gotchas (don't relearn these)
- **The old Lead Track file's columns 5–6 (Opp ID, Case Manager_C) are misaligned with its rows.** Never use them.
- **Some SF `Mobile__c` values are placeholders** (949-xxx-1111, -8888, -6666). The real number is in `Phone__c`. During the
  one-lead test this overwrote a real, already-dialed number (reverted at once). Placeholders are now ignored, including any number on
  3+ clients (three clients all had the same 949-xxx-6677).
- **A client record can hold a staff number.** One client had a staff member's number in both SF phone fields.
  Every run now reads all SF user phones and never uses them for a client (and refuses to plan if it can't read them).
- The Convoso custom field `file` comes back from the API as **`field_43`** (`client_name` = field_19, `case_manager` = field_20).
- The old weekly upload made **a new lead for the same client every week**, so call history reset weekly. The sync keeps one lead per client.
- `leads/update` with a `list_id` in **another campaign** moves the lead and keeps its id and call history (proven 10/27).
- The case manager field in SF is set at **Build Plan** (in sales), so "has a CM" does not mean "is a client".

## 10. Decision log
| Date | Decision |
|---|---|
| 2026-10-27 | SF replaces the Lead Track file. Convoso structure unchanged. Daily, weekdays |
| 2026-10-27 | Client = Contract Signed until Cancelled, incl. NSF/EFT. Cancelled = not a DCG client |
| 2026-10-27 | Removed leads go to the campaign's newest inactive list older than its active list |
| 2026-10-27 | DNC always respected; DNC'd clients stay off |
| 2026-10-27 | 5 PM plan with Socket Mode buttons, anyone can press, auto-sync at 6 PM if nobody rejects |
| 2026-10-27 | SF is the source of truth for phones; placeholder and shared office numbers never used. Duplicate leads: keep one |
| 2026-10-27 | Watchdog in the savva pinger; alerts by Slack DM |

## 11. Open items
- **Back office should stop dropping the Thursday Lead Track file** (the old `cm\` bot would create competing lists).
- **Stefan Rivera:** retire his campaign if he has left.
- **Kalyssa:** rename or replace the legacy Asmeri list as her active list (optional, cosmetic).
- **Eli / back office:** fix the "needs a person" items in SF (wrong, missing or duplicate numbers), plus the 4 placeholder mobiles.
- **Slack thread is long** (one line per change, split over many messages). Planned: a short "needs a person" message plus one file
  (needs the `files:write` scope on the CM bot app). DNC to become an info line, not a warning.
