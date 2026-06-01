# Azure VM Scheduler / Idle Reaper

Ansible role + playbooks to allocate, deallocate, schedule, and auto-reap idle Azure VMs. Runs on demand or off AAP/AWX schedules.

> **Just want to allocate / deallocate / schedule?** Use the runbook: [`azure_vm_runbook.yml`](#runbook-allocate--deallocate--schedule). It exposes those three actions directly. The rest of this README covers the underlying role and the idle-reaper.

## Why

Azure's built-in auto-shutdown (`Microsoft.DevTestLab/schedules`) is purely schedule-based, it can't react to idleness. Azure Automation's "Start/Stop VMs during off-hours" works but lives outside the rest of our AAP/Ansible automation, which makes auditing it a pain. This role keeps VM lifecycle in the same control loop as everything else.

## Modes

| Mode | Runbook action | What it does |
|------|----------------|--------------|
| `start` | `allocate` | Allocate + power on. Idempotent, skips already-running VMs. |
| `stop` | `deallocate` | Deallocate (releases compute, stops compute billing). Skips already-deallocated VMs. |
| `schedule` | `schedule` | Reconciles each VM to a recurring weekly window: allocates inside business hours, deallocates outside them. DST-correct. See [Schedule mode](#schedule-mode). |
| `idle_check` | — | Queries Azure Monitor for CPU + network (and optionally disk IO), checks against thresholds, deallocates if idle for the full window. |

Note: deallocate is not the same as "stop". A stopped VM still bills for compute. This role always deallocates.

## Repo layout

```
.
├── ansible.cfg
├── requirements.yml
├── azure_vm_runbook.yml            # runbook: allocate | deallocate | schedule
├── inventory/azure_rm.yml          # dynamic inventory, all VMs in sub
├── group_vars/all.yml              # tunables (thresholds, schedule, tags, auth)
├── playbooks/manage_vms.yml        # role entry point (start/stop/schedule/idle_check)
└── roles/azure_vm_manager/
    ├── defaults/main.yml
    ├── meta/main.yml
    └── tasks/
        ├── main.yml                # dispatcher
        ├── start.yml
        ├── stop.yml
        ├── schedule.yml            # weekly-window allocate/deallocate decision
        ├── idle_check.yml          # Azure Monitor query + decision
        ├── get_token.yml           # SP or MSI bearer token
        └── notify.yml              # webhook hook
```

## Setup

### 1. Install collections

```bash
ansible-galaxy collection install -r requirements.yml
pip install 'ansible[azure]'   # or rely on AAP execution environment
```

### 2. Service Principal permissions

The SP (or the controller's Managed Identity) needs the following, scoped to the subscription or to the target RGs:

| Role | Why |
|------|-----|
| `Virtual Machine Contributor` | start/deallocate operations |
| `Monitoring Reader` | `Microsoft.Insights/metrics/read` for idle detection |

`Reader` alone is not enough. It gives you read on resources but not on the metrics endpoints.

### 3. Tag your VMs

Opt-in (default):
```
AutoStop = true
```

Hard opt-out (overrides everything):
```
AutoStopExclude = <any value>
```

If you'd rather not bother with the opt-in tag, set `autostop_tag_key: ""` in `group_vars/all.yml` and the role will run against everything in `target_hosts`.

## Runbook: allocate / deallocate / schedule

`azure_vm_runbook.yml` is the front door for the three lifecycle actions. Pick the action with `-e action=...`:

```bash
# Allocate (start) one VM on demand
ansible-playbook azure_vm_runbook.yml -e action=allocate -e target_hosts=jumphost-prod-01

# Deallocate a whole resource group (releases compute, stops compute billing)
ansible-playbook azure_vm_runbook.yml -e action=deallocate -e target_hosts=rg_dev_eastus

# Schedule sweep: reconcile every AutoSchedule=true VM to its window.
# Run this on a cron / AAP schedule (every 15–30 min).
ansible-playbook azure_vm_runbook.yml -e action=schedule

# Preview a schedule sweep without changing anything
ansible-playbook azure_vm_runbook.yml -e action=schedule -e notify_only=true
```

| `action` | Effect | Host gating |
|----------|--------|-------------|
| `allocate` | Allocate + power on | exclusion tag only (you choose hosts via `target_hosts`) |
| `deallocate` | Deallocate | exclusion tag only |
| `schedule` | Reconcile to the weekly window | exclusion tag **and** opt-in `AutoSchedule=true` |

All three honor `dry_run` (runs the ARM call in check_mode) and the `AutoStopExclude` hard opt-out. `schedule` also honors `notify_only` (evaluate + report, never change state).

The runbook is a thin wrapper over the `azure_vm_manager` role, so `playbooks/manage_vms.yml -e operation_mode=schedule` does the same thing if you prefer the role's native verbs.

## Schedule mode

Schedule mode answers "should this VM be running *right now*?" from a recurring weekly window, then reconciles to it — allocate inside the window, deallocate outside it. Unlike Azure's DevTestLab auto-shutdown it can also **start** VMs, and unlike a plain cron it makes an idempotent decision every run (so a missed run self-heals on the next one).

**DST-correct.** The decision reads the current weekday + time *in the target timezone* (`TZ=… date`) rather than doing fixed UTC-offset math, so the window automatically follows daylight-saving changes. Set `schedule_timezone` to the business's timezone (e.g. `America/New_York`), not UTC, if you want that behavior.

### Defaults (`group_vars/all.yml`)

```yaml
schedule_tag_key: "AutoSchedule"   # opt-in tag; "" = every host in target_hosts
schedule_tag_value: "true"
schedule_timezone: "UTC"
schedule_business_start: "08:00"   # allocate at/after this local time
schedule_business_end:   "18:00"   # deallocate at/after this local time
schedule_business_days:  "1-5"     # ISO weekdays 1=Mon..7=Sun ("1-5" or "1,2,3,4,5")
```

### Per-VM overrides (tags)

Any VM can override the global window with tags. Most-specific wins:

| Tag | Example | Meaning |
|-----|---------|---------|
| `AutoSchedule` | `true` | Opt in to scheduling (required unless `schedule_tag_key` is `""`) |
| `ScheduleStart` | `08:00` | Local start of the running window |
| `ScheduleStop` | `18:00` | Local end of the running window |
| `ScheduleDays` | `1-5` | Days the window opens (ISO; range or CSV) |
| `ScheduleTZ` | `America/New_York` | Timezone the times are interpreted in |

**Overnight windows** are supported: set `ScheduleStop` earlier than `ScheduleStart` (e.g. `20:00`→`06:00`) to keep a VM up across midnight. `ScheduleDays` then refers to the day the window *opens*; the early-morning tail is governed by the previous day's membership. `start == stop` means "never run".

Because the decision is recomputed every run, **how often you schedule the sweep is your resolution** — run it every 15–30 min so a VM comes up within that of its start time. See the AAP table below for a ready-to-go job template.

## Usage

### CLI

```bash
# Deallocate everything tagged AutoStop=true
ansible-playbook playbooks/manage_vms.yml -e operation_mode=stop

# Start one VM on demand
ansible-playbook playbooks/manage_vms.yml \
  -e operation_mode=start \
  -e target_hosts=jumphost-prod-01

# Idle sweep, report-only first so you can validate thresholds
ansible-playbook playbooks/manage_vms.yml \
  -e operation_mode=idle_check \
  -e notify_only=true

# Tighter idle policy: 30 min window, 3% CPU, 512 KiB network
ansible-playbook playbooks/manage_vms.yml \
  -e operation_mode=idle_check \
  -e idle_window_minutes=30 \
  -e idle_cpu_threshold_percent=3.0 \
  -e idle_network_threshold_bytes=524288
```

### AAP / AWX

Three job templates against this project, all using the **Microsoft Azure Resource Manager** credential type:

| Job Template | Playbook | Extra vars | Survey | Schedule |
|--------------|----------|------------|--------|----------|
| `azure-vm-allocate` | `azure_vm_runbook.yml` | `action: allocate` | `target_hosts` (text) | none, on demand |
| `azure-vm-deallocate` | `azure_vm_runbook.yml` | `action: deallocate` | `target_hosts` (text, default `tag_AutoStop_true`) | cron, nights/weekends |
| `azure-vm-scheduler` | `azure_vm_runbook.yml` | `action: schedule` | `notify_only` (bool, default false) | cron, every 15–30 min |
| `azure-vm-idle-reaper` | `playbooks/manage_vms.yml` | `operation_mode: idle_check` | see below | cron, every 30 min |

The `azure-vm-scheduler` template lets the per-VM `Schedule*` tags (and the `schedule_*` defaults) decide allocate-vs-deallocate, so a single recurring schedule covers an entire mixed fleet. Tag VMs that should never be auto-scheduled with `AutoStopExclude`, and opt the rest in with `AutoSchedule=true`.

Point AAP's inventory source at `azure_rm.yml` from this repo. On AAP 2.6, keep `cache_timeout: 300` low so newly-tagged VMs show up on the next run.

### Surveys to expose

For `azure-vm-idle-reaper` add a survey with these prompted fields (all optional, override defaults):

- `idle_window_minutes` (int, default 60)
- `idle_cpu_threshold_percent` (float, default 5.0)
- `notify_only` (bool, default false)
- `dry_run` (bool, default false)

## Idle detection

A VM is flagged idle when all of the following hold over the look-back window:

1. CPU avg < `idle_cpu_threshold_percent` (default 5%)
2. Network In Total + Network Out Total < `idle_network_threshold_bytes` (default 1 MiB)
3. (Optional, when `check_disk_io: true`) Disk Read+Write Total < `idle_disk_io_threshold_bytes`
4. Uptime guard: at least 80% of the expected metric data points came back (catches VMs that started part-way through the window)

If any of those fail, no action is taken.

## Things to watch out for

A few cases worth knowing about before you flip this on:

- VM started mid-window: the uptime guard refuses to dealloc until a full window of data exists.
- Metrics API returns no data: treated the same as "uptime guard failed", the VM is left alone.
- VM already in the target state: start/stop short-circuit, the calls are idempotent anyway.
- VM tagged for exclusion: `end_host` in `pre_tasks` skips it before any API call.
- AAD token rate-limit on large fleets: the token is acquired once per play (`run_once`) and reused across all hosts. The Metrics API itself is roughly 12k req/h/subscription, so at 25 forks * 1 call/host you can sweep ~12k VMs/hour. Tune `parallel_forks` down if you push past that.
- Transient ARM 429/503: start/stop/dealloc are wrapped in 3x retry with 15s backoff.
- Active SSH sessions on otherwise-idle hosts: keepalive traffic almost always crosses 1 MiB/hour, so they won't false-positive. If your jump-host policy is "stay up while anyone's connected", raise `idle_network_threshold_bytes` or add a log-based check.

Things this role does *not* try to deal with:

- VMSS (scale set) instances - different module (`azure_rm_virtualmachinescaleset`). Scale-out/in is a different problem.
- Memory pressure: Azure Monitor doesn't expose guest memory without the Azure Monitor Agent + a Data Collection Rule. A RAM-bound but CPU-idle VM *will* get deallocated. Tag those `AutoStopExclude`.
- Active RDP: same caveat as SSH, RDP heartbeat is small, raise the network threshold if you need to.
- Reservation / Spot eligibility: deallocating then re-allocating a Spot VM means Azure may not give you capacity back. Tag Spot fleets `AutoStopExclude`.

## Other options we looked at

- `Microsoft.DevTestLab/schedules` (Auto-Shutdown): fine for pure time-of-day shutdown, but no idle detection and no programmatic re-start.
- Azure Automation Start/Stop V2: nice UI in the portal, but lives outside AAP. Harder to audit alongside Ansible runs.
- Azure Policy + Logic App: event-driven, no controller needed, but more moving parts and the idle logic ends up living in Log Analytics queries.

We went with the role because it sits inside our existing Ansible/AAP automation, gives us custom idle logic, and the full audit trail lands in AAP job output. Downside: needs AAP/AWX to schedule and is more code to maintain than DTL.

## Pre-prod checklist

1. Run with `notify_only: true` for a full week, then review the AAP job logs.
2. Make sure nothing tagged `AutoStopExclude` ever shows `would_deallocate`.
3. Confirm jump hosts, AD DCs, and DB primaries are excluded.
4. Confirm the SP can actually hit the metrics endpoint: run idle_check against one known-running VM with `dry_run: true` and check the debug line shows non-zero cpu/net values.
5. Flip `notify_only: false`, keep `dry_run: false`, schedule it.
