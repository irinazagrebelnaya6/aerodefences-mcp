"""
Інтерактивний тестер для server_aerodefences.py (БЕЗ LLM).

Піднімає сервер і дає простий REPL, де знаряддя викликаються вручну.
Команди:
  tools                     - список знарядь
  schema                    - прочитати resource://schema (перші рядки)
  call <tool> {json-args}   - викликати знаряддя з аргументами
  quit                      - вихід

Приклади:
  call list_products {"limit": 3}
  call find_products {"category": "sensors"}
  call get_product {"slug": "battlecore-fc7"}
  call set_product_status {"slug": "edgenode-ai", "status": "archived"}
"""

import asyncio
import json

from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.logging import LogMessage


async def log_handler(message: LogMessage):
    extra = message.data.get("extra")
    tail = f" | {extra}" if extra else ""
    print(f"  [SERVER {message.level.upper()}] {message.data.get('msg')}{tail}")


async def progress_handler(progress, total, message):
    pct = f"{progress/total*100:.0f}%" if total else str(progress)
    print(f"  progress: {pct}")


async def elicitation_handler(message, response_type, params, context):
    # Тут stdin ДОСТУПНИЙ, тому підтвердження запитується по-справжньому
    print(f"\n  [SERVER ПИТАЄ] {message}")
    ans = input("  yes/no (Enter=decline, cancel=abort) > ").strip()
    if ans.lower() == "cancel":
        return ElicitResult(action="cancel")
    if ans == "":
        return ElicitResult(action="decline")
    if response_type is None:
        return ElicitResult(action="accept")
    return response_type(value=ans)


async def main():
    client = Client(
        "./server_aerodefences.py",
        log_handler=log_handler,
        progress_handler=progress_handler,
        elicitation_handler=elicitation_handler,
    )

    async with client:
        print("Підключено. Команди: tools | schema | call <tool> {json} | quit\n")
        loop = asyncio.get_event_loop()
        while True:
            try:
                line = (await loop.run_in_executor(None, input, "mcp> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue

            if line in ("quit", "exit", "q"):
                break

            if line == "tools":
                for t in await client.list_tools():
                    print(f"  - {t.name}")
                continue

            if line == "schema":
                res = await client.read_resource("resource://schema")
                data = json.loads(res[0].text)
                print(f"  база {data['database']}, таблиць {len(data['tables'])}:")
                print("  " + ", ".join(data["tables"].keys()))
                continue

            if line.startswith("call "):
                rest = line[len("call "):].strip()
                parts = rest.split(" ", 1)
                tool = parts[0]
                try:
                    args = json.loads(parts[1]) if len(parts) > 1 else {}
                except json.JSONDecodeError as e:
                    print(f"  ! кепський JSON: {e}")
                    continue
                try:
                    result = await client.call_tool(tool, args)
                    print("  RESULT:", json.dumps(result.data, ensure_ascii=False,
                                                  indent=2, default=str)[:2000])
                except Exception as e:
                    print(f"  ! помилка: {e}")
                continue

            print("  ? невідома команда")


if __name__ == "__main__":
    asyncio.run(main())
