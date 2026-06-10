"""
modes/ — one module per CLI mode, extracted from main.py (B13).

main.py stays a thin dispatcher: parse args, gate live mode, hand off
to the right mode module.

| Module          | CLI flag            | What it runs                          |
|-----------------|---------------------|---------------------------------------|
| intelligence.py | (via --btc5m)       | OBIApp — single-token Rich dashboard   |
| btc5m.py        | --btc5m             | Auto-rotating 5m windows around OBIApp |
| select.py       | (interactive menus) | Market pickers shared by modes         |

Pairs / headless / record / synthetic remain inline in main() — each is
a short wiring block around classes that live in execution/ and ui/.
"""
