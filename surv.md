# How to Build the Survey (AAP 2.6)

The survey lives on the **Job Template**, not in the repo — AAP does not read
`survey.json` on project sync. Either **(A)** create the questions by hand using
the table below, or **(B)** import `survey.json` via the CLI.

These questions feed `azure_vm_lifecycle.yml`. The **variable names must match
exactly** — that is how each answer reaches the playbook.

---

## A. Create the questions by hand

1. Automation Execution → **Templates** → open **Azure VM Lifecycle**.
   (The template must already exist and be saved.)
2. Open the **Survey** tab → toggle **Survey** ON.
3. Click **Add question** and fill in each question below, in order. Click
   **Save** after each, then **Save** the survey when done. Leave the Survey
   toggle ON.

> Add questions in this order so the launch prompt reads top-to-bottom:
> Action → Run mode → Schedule name → Days → Time → Timezone.

### Q1 — Action
| Field | Value |
|---|---|
| Question | `Action` |
| Description | `Allocate (start), deallocate, or report status` |
| Answer variable name | `vm_action` |
| Answer type | **Multiple Choice (single select)** |
| Multiple Choice Options (one per line) | `start`<br>`deallocate`<br>`status` |
| Default answer | `status` |
| Required | ✅ Yes |

### Q2 — Run mode
| Field | Value |
|---|---|
| Question | `Run mode` |
| Description | `Run now (adhoc) or create a recurring schedule` |
| Answer variable name | `run_mode` |
| Answer type | **Multiple Choice (single select)** |
| Multiple Choice Options | `adhoc`<br>`schedule` |
| Default answer | `adhoc` |
| Required | ✅ Yes |

### Q3 — Schedule name
| Field | Value |
|---|---|
| Question | `Schedule name (schedule mode)` |
| Description | `Name for the AAP Schedule. Required when Run mode = schedule.` |
| Answer variable name | `schedule_name` |
| Answer type | **Text** |
| Default answer | `vmlab-deallocate` |
| Required | ❌ No |

### Q4 — Days
| Field | Value |
|---|---|
| Question | `Days (schedule mode)` |
| Description | `Day(s) of the week the schedule runs.` |
| Answer variable name | `schedule_days` |
| Answer type | **Multiple Choice (multiple select)** |
| Multiple Choice Options (one per line) | `Monday`<br>`Tuesday`<br>`Wednesday`<br>`Thursday`<br>`Friday`<br>`Saturday`<br>`Sunday` |
| Default answer (pre-selected) | `Monday`, `Tuesday`, `Wednesday`, `Thursday`, `Friday` |
| Required | ❌ No |

> For a **multiple-select** question, the Default is the set of options to
> pre-check. In the raw spec it's newline-separated (see `survey.json`).

### Q5 — Time (24-hour HHMM)
| Field | Value |
|---|---|
| Question | `Time 24h HHMM (schedule mode)` |
| Description | `Time of day in 24-hour HHMM, e.g. 1800 = 6:00 PM.` |
| Answer variable name | `schedule_time` |
| Answer type | **Text** |
| Default answer | `1800` |
| Required | ❌ No |

### Q6 — Timezone
| Field | Value |
|---|---|
| Question | `Timezone (schedule mode)` |
| Description | `America/New_York covers EST/EDT automatically.` |
| Answer variable name | `schedule_timezone` |
| Answer type | **Multiple Choice (single select)** |
| Multiple Choice Options | `America/New_York`<br>`America/Chicago`<br>`America/Denver`<br>`America/Los_Angeles`<br>`UTC` |
| Default answer | `America/New_York` |
| Required | ❌ No |

4. **Save** the survey; confirm the **Survey** toggle is ON.

---

## B. Import instead of clicking

If you have the AAP/AWX CLI, load all six questions from `survey.json`:

```bash
awx job_template modify "Azure VM Lifecycle" \
  --survey_enabled true \
  --survey_spec @survey.json
```

(or via the API: `POST /api/v2/job_templates/<id>/survey_spec/` with the
contents of `survey.json`.)

---

## Notes

- **Host selection is NOT a survey question.** Operators pick hosts with the
  Job Template's **Limit** field (Prompt on launch) against the dynamic
  inventory. AAP surveys cannot render a live host checklist.
- **Q3–Q6 only matter when `run_mode = schedule`.** In adhoc mode they are
  ignored. The playbook turns Days + Time + Timezone into the schedule's RRULE.
- If you only want allocate/deallocate with no scheduling, create just **Q1**
  (and optionally Q2); the playbook defaults `run_mode` to `adhoc`.
