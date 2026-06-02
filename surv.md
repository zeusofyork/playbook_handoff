{
  "name": "Azure VM Lifecycle Survey",
  "description": "Choose allocate/deallocate, run now or on a schedule. Host selection is the Job Template's Limit field (prompt on launch). Pairs with azure_vm_lifecycle.yml.",
  "spec": [
    {
      "question_name": "Action",
      "question_description": "Allocate (start), deallocate, or report status of the hosts chosen via Limit.",
      "variable": "vm_action",
      "type": "multiplechoice",
      "choices": ["start", "deallocate", "status"],
      "default": "status",
      "required": true
    },
    {
      "question_name": "Run mode",
      "question_description": "Run the action now (adhoc) or create a recurring AAP schedule.",
      "variable": "run_mode",
      "type": "multiplechoice",
      "choices": ["adhoc", "schedule"],
      "default": "adhoc",
      "required": true
    },
    {
      "question_name": "Schedule name (schedule mode)",
      "question_description": "Name for the AAP Schedule. Required when Run mode = schedule.",
      "variable": "schedule_name",
      "type": "text",
      "default": "vmlab-deallocate",
      "required": false
    },
    {
      "question_name": "Days (schedule mode)",
      "question_description": "Pick the day(s) of the week the schedule runs. Required when Run mode = schedule.",
      "variable": "schedule_days",
      "type": "multiselect",
      "choices": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
      "default": "Monday\nTuesday\nWednesday\nThursday\nFriday",
      "required": false
    },
    {
      "question_name": "Time 24h HHMM (schedule mode)",
      "question_description": "Time of day in 24-hour HHMM, e.g. 1800 for 6:00 PM. Required when Run mode = schedule.",
      "variable": "schedule_time",
      "type": "text",
      "default": "1800",
      "required": false
    },
    {
      "question_name": "Timezone (schedule mode)",
      "question_description": "Timezone for the schedule. America/New_York covers EST/EDT automatically.",
      "variable": "schedule_timezone",
      "type": "multiplechoice",
      "choices": ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "UTC"],
      "default": "America/New_York",
      "required": false
    }
  ]
}
