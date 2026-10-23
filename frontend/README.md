# SubCan Frontend Dashboard

**Owner:** Dadu  
**Status:** Implemented static dashboard

## Overview

The dashboard at `/dashboard/` provides:
1. Overview of tracked streaming subscriptions and monthly expenditure.
2. Inactivity alerts and eligibility candidates (unused for >= 3 calendar months).
3. Evidence viewer displaying usage history, cancellation terms, and consequence disclosures (e.g. data loss warnings, pause alternatives).
4. **Approval Control Center**: Explicit per-subscription review modal where users can safely approve or decline cancellation.
5. Verification status feed showing independent confirmation that cancellations actually took effect.

## Integration

1. The static client uses the backend API; it does not contain subscription or outcome data.
2. **Contracts to Follow:**
   - Consume endpoints specified in [docs/API_CONTRACT.md](file:///docs/API_CONTRACT.md).
   - Use field names, data types, and status enums defined in [docs/DATA_CONTRACT.md](file:///docs/DATA_CONTRACT.md).
3. **Safety Requirement:**
   - The approval interface must never offer a "Select All & Cancel" bulk button.
   - Each subscription cancellation must be reviewed and approved individually with full disclosure of provider consequences.
4. Start the API with `uvicorn backend.app.main:app --reload`, then open `http://127.0.0.1:8000/dashboard/`.
