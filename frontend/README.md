# SubCan Frontend (Phase 0 Placeholder)

**Owner:** Dadu  
**Status:** Phase 0 Placeholder  

## Overview

The SubCan Frontend dashboard will provide users with:
1. Overview of tracked streaming subscriptions and monthly expenditure.
2. Inactivity alerts and eligibility candidates (unused for >= 3 calendar months).
3. Evidence viewer displaying usage history, cancellation terms, and consequence disclosures (e.g. data loss warnings, pause alternatives).
4. **Approval Control Center**: Explicit per-subscription review modal where users can safely approve or decline cancellation.
5. Verification status feed showing independent confirmation that cancellations actually took effect.

## Guidelines for Dadu

1. **Phase 0 Constraint:** Do not implement a React or frontend framework during Phase 0.
2. **Contracts to Follow:**
   - Consume endpoints specified in [docs/API_CONTRACT.md](file:///docs/API_CONTRACT.md).
   - Use field names, data types, and status enums defined in [docs/DATA_CONTRACT.md](file:///docs/DATA_CONTRACT.md).
3. **Safety Requirement:**
   - The approval interface must never offer a "Select All & Cancel" bulk button.
   - Each subscription cancellation must be reviewed and approved individually with full disclosure of provider consequences.
4. **Branch:**
   - Conduct frontend development on `feature/dadu-database-ui`.
