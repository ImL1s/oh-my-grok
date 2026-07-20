# Live suite evidence (local only)

Machine-local canary / suite logs are **not** shipped in the public tree.

Generate on a developer machine:

```bash
# from repo root
./scripts/canary_pretool.py --dry
# optional live (needs grok):
# python3 scripts/canary_pretool.py --live
# ./scripts/live_suite.sh
```

Outputs land under this directory (gitignored except this README).
