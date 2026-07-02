## What & why

## Checklist
- [ ] Backend tests green (`cd backend && pytest -q`)
- [ ] Frontend tests green (`cd frontend-v2 && npm run test:run`)
- [ ] Model changed → Alembic migration included
- [ ] Architecture changed → `docs/ARCHITECTURE.md` + ADR updated
- [ ] No secrets, personal data, or machine-specific absolute paths
      (use env vars — see `docs/decisions/043-open-source-release-contract.md`)
