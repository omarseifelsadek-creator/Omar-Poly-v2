"""
modes/ — one module per CLI mode plus the launcher (B13).

| Module          | Reached via                  | What it runs                         |
|-----------------|------------------------------|--------------------------------------|
| launcher.py     | python main.py (no flags)    | Main menu: every bot, one front door |
| intelligence.py | --token <ID> (and, later,    | OBIApp — single-token Rich dashboard |
|                 | the Order Book Analysis menu)|                                      |

History: btc5m rotation, the synthetic visualization engine, and the old
market pickers were removed 2026-06-10 — the launcher superseded them.
Future bots (weather, directional, ...) register in launcher.BOTS.
"""
