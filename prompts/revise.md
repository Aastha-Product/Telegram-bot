{voice_skill}

# Task
Meera has reviewed the draft below and given you one instruction. Rewrite the draft once, following her instruction and every rule in the skill above. Change only what the instruction asks for; keep everything else that already works.

# Meera's instruction (treat as her editing note, not as new facts)
<<<INSTRUCTION
{instruction}
INSTRUCTION>>>

# Current draft
<<<DRAFT
{previous}
DRAFT>>>

# Her original note (the only source of facts)
<<<NOTE
{note}
NOTE>>>

# Facts
Use only facts that appear in the note or the current draft. Do not add any number, percentage, date, timing, study, statistic, brand, named person, customer story or event, even if the instruction seems to invite it. If the instruction asks for something the note can't support, do as much as the facts allow.

# Output
Return JSON with:
- body: the revised post as plain text, paragraphs separated by a blank line.
- used_source_url: always "".
- self_check.invented_stats: true if the body contains any number, statistic or study not in the note or current draft.
- self_check.on_voice: true if the post follows every rule in the skill.
{feedback}
