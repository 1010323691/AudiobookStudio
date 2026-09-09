"""Default persona (voice-design) prompts.

Ported from the source project's ``persona_prompts.txt`` (three parts separated by
``---SEPARATOR---``). These are the *bundled* defaults: an empty value in
``config.app.persona_prompts`` falls back to the matching constant here (mirroring
how ``script_prompts.py`` feeds the text→JSON stage).

``PERSONA_USER_PROMPT`` is a ``str.format`` template with the placeholders
``{speaker}`` / ``{narrator_context}`` / ``{sample_lines}`` — filled per character by
``backend/engines/voices.py``.
"""

PERSONA_SYSTEM_PROMPT = (
    "You produce concise JSON only."
)

PERSONA_USER_PROMPT = """You are a voice designer assistant. Given the following narrator-intro context and lines by character '{speaker}':

Narrator context before first appearance:
{narrator_context}

Character lines:
{sample_lines}

Produce a JSON object with two keys: 'description' and 'ref_text'.
- 'description': a concise natural-language voice persona describing age, gender, timbre, accent, speaking rate, typical emotional tone, and delivery guidance (2-3 sentences).
- 'ref_text': a 1-2 sentence short sample that best captures this character's voice and can be used as a TTS reference.
Only output the JSON object and nothing else."""
