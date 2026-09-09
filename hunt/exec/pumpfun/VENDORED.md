# Vendored: pumpfun-python (v0.2.0) — MIT

Source: https://github.com/JinUltimate1995/pumpfun-python
Commit pinned at review time: package dir `pumpfun/` as of ~Sep 2026.

Why vendored: exact PumpFun bonding-curve v2 buy/sell protocol bytes (17-account
BUY, 15-account SELL incl. the creator_vault/token_program SWAP quirk) verified
against production usage. PyPI `pumpdotfun` failed to install on Python 3.14 and
is a third-party wrapper; this pure solders+httpx implementation is safer and
reviewable. It builds UNSIGNED instructions/messages only — keys never enter
this library.

Changes from upstream: none (kept byte-for-byte to stay faithful to the verified
protocol). All callers live in `hunt/exec/live.py`.