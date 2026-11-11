# Data License — CC BY 4.0

The dataset in `data/mcp_data.json` (Wenjia home-fix diagnosis trees) is licensed under
the Creative Commons Attribution 4.0 International License.

You are free to share and adapt the data for any purpose, including commercial,
provided you give appropriate credit.

**Required attribution:**
> 问家. 问家家庭维修诊断树 v0.1.0 [数据集]. 2026-11-10. CC BY 4.0.

or in English:
> Wenjia. Home-Fix Diagnosis Trees v0.1.0 [dataset]. 2026-11-10. CC BY 4.0.

License text: https://creativecommons.org/licenses/by/4.0/

## Restrictions that follow from the data's purpose

These are not extra license terms — they are usage requirements stated in the dataset's
datasheet, and we ask redistributors to honor them:

1. Do not strip the `see_pro` field (when to stop and call a licensed technician) or the
   safety ordering of `fix[]` steps.
2. Do not present `prob_prior` as measured frequencies; it is a ranking prior.
3. Gas, electrical, height and structural work must not be automated without a human
   confirmation step.
