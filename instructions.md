1. Open the Job Template

  Automation Execution → Templates → your Azure VM Lifecycle template (create it first if needed: playbook
  azure_vm_lifecycle.yml, your Azure inventory, Azure credential + AAP credential, and Limit → ✅ Prompt on launch). Save it
  once so the Survey tab is available.

  2. Enable the survey

  Open the template → Survey tab → toggle Survey on → click Add question for each of the four below.

  3. Add these four questions (field-by-field)

  Q1 — Action
  - Question: Action
  - Description: Allocate (start), deallocate, or report status
  - Answer variable name: vm_action
  - Answer type: Multiple Choice (single select)
  - Choices (one per line): start, deallocate, status
  - Default answer: status
  - Required: ✅

  Q2 — Run mode
  - Question: Run mode
  - Description: Run now (adhoc) or create a recurring schedule
  - Answer variable name: run_mode
  - Answer type: Multiple Choice (single select)
  - Choices: adhoc, schedule
  - Default answer: adhoc
  - Required: ✅

  Q3 — Schedule name
  - Question: Schedule name (schedule mode)
  - Answer variable name: schedule_name
  - Answer type: Text
  - Default answer: vmlab-deallocate-nightly
  - Required: ❌

  Q4 — Schedule RRULE
  - Question: Schedule RRULE (schedule mode)
  - Answer variable name: schedule_rrule
  - Answer type: Text
  - Default answer: DTSTART:20260602T190000Z RRULE:FREQ=DAILY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR
  - Required: ❌

  4. Save

  Save each question, then Save the survey, and make sure the Survey toggle is ON on the template.

  ---
  How operators then use it (the flow you wanted)
  
  At Launch: set Limit = the host(s) (e.g. vm-aap-test-ctr-001.jaxnavy.org_dc55) → answer Action + Run mode → if schedule,
  fill the schedule name/RRULE. Done.

  ▎ The variable names must match exactly (vm_action, run_mode, schedule_name, schedule_rrule) — that's how the survey feeds 
  ▎ the playbook.

  Faster alternative — import instead of clicking

  If you have the awx/AAP CLI, you can load it from survey.json instead of typing all four:
  awx job_template modify "Azure VM Lifecycle" \
    --survey_enabled true --survey_spec @runner/project/survey.json
    
  Two notes:
  - Host selection is the Limit field, not a survey question (that's the AAP limitation we covered) — so there's intentionally
  no "hosts" question in the survey.
  - If you'd rather only expose allocate/deallocate (no scheduling), just add Q1 and skip Q2–Q4; the playbook defaults
  run_mode to adhoc.
  
  Want me to also walk through creating the Schedule (for run_mode=schedule, or the native AAP schedule approach)?

✻ Worked for 28s · 1 shell still running

