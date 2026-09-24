"""Real-call smoke test (not part of pytest). Usage: python smoke_gemini.py [audio_file]"""

import asyncio
import mimetypes
import sys
from pathlib import Path

import config
import gemini_client

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}, "echo": {"type": "string"}},
          "required": ["ok", "echo"]}


async def main() -> None:
    reply = await gemini_client.generate_json('Return ok=true and echo="skinstinct".', SCHEMA,
                                              config.settings.triage_model)
    print(f"generate_json ({config.settings.triage_model}):", reply)
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        mime = mimetypes.guess_type(path.name)[0] or gemini_client.TELEGRAM_VOICE_MIME
        text = await gemini_client.transcribe_audio(path.read_bytes(), mime, config.settings.transcribe_model)
        print(f"transcribe_audio ({config.settings.transcribe_model}):", text)


if __name__ == "__main__":
    asyncio.run(main())
