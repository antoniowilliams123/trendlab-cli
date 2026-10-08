
## 2026-10-08 01:44 UTC — suite deepseek:deepseek-flash@harness

| task | lang | model | profile | located | root_cause | passes | no_collateral | regression_added | interventions | status | cost | tokens_lead | tokens_total | wall_s | guards_fired | verification | stop_reason |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| py01-off_by_one | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0213 | 69224 | 74736 | 59.2 | {} | pass | None |
| py02-wrong_operator | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0379 | 66994 | 76692 | 100.4 | {} | pass | None |
| py03-missing_none_check | python | deepseek:deepseek-flash | harness | True | True | False | True | True | 0 | COMPLETED | 0.0161 | 90485 | 94474 | 48.4 | {} | pass | None |
| py05-wrong_import | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0221 | 77236 | 82811 | 65.2 | {} | pass | None |
| py07-config_typo | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0246 | 59897 | 65972 | 69.3 | {} | pass | None |
| py10-wrong_operator | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0147 | 66292 | 70055 | 43.7 | {} | pass | None |
| ts01-off_by_one | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0288 | 85605 | 92665 | 83.8 | {} | pass | None |
| ts02-wrong_operator | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0052 | 37411 | 38515 | 15.7 | {} | pass | None |
| ts03-missing_none_check | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0212 | 59739 | 64897 | 58.8 | {} | pass | None |
| ts04-async_misuse | typescript | deepseek:deepseek-flash | harness | True | True | True | False | True | 0 | COMPLETED | 0.2417 | 850759 | 871754 | 525.4 | {'verifier_fix_round': 1} | fix | None |

## 2026-10-08 01:47 UTC — suite deepseek:deepseek-flash@bare

| task | lang | model | profile | located | root_cause | passes | no_collateral | regression_added | interventions | status | cost | tokens_lead | tokens_total | wall_s | guards_fired | verification | stop_reason |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| py01-off_by_one | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0036 | 34436 | 34436 | 12.0 | {} | None | None |
| py02-wrong_operator | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0034 | 29733 | 29733 | 10.3 | {} | None | None |
| py03-missing_none_check | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0086 | 67214 | 67214 | 29.9 | {} | None | None |
| py05-wrong_import | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0037 | 36466 | 36466 | 13.2 | {} | None | None |
| py07-config_typo | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0048 | 61951 | 61951 | 18.0 | {} | None | None |
| py10-wrong_operator | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0031 | 38186 | 38186 | 12.1 | {} | None | None |
| ts01-off_by_one | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0046 | 42244 | 42244 | 15.2 | {} | None | None |
| ts02-wrong_operator | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0036 | 29574 | 29574 | 12.2 | {} | None | None |
| ts03-missing_none_check | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0052 | 60374 | 60374 | 19.5 | {} | None | None |
| ts04-async_misuse | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 1 | COMPLETED | 0.0059 | 67898 | 67898 | 23.1 | {} | None | None |

## 2026-10-08 01:50 UTC — suite deepseek:deepseek-v4-pro@bare

| task | lang | model | profile | located | root_cause | passes | no_collateral | regression_added | interventions | status | cost | tokens_lead | tokens_total | wall_s | guards_fired | verification | stop_reason |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| py01-off_by_one | python | deepseek:deepseek-v4-pro | bare | True | True | True | True | True | 0 | COMPLETED | 0.0156 | 36520 | 36520 | 23.5 | {} | None | None |
| py02-wrong_operator | python | deepseek:deepseek-v4-pro | bare | True | True | True | True | True | 0 | COMPLETED | 0.021 | 73263 | 73263 | 28.2 | {'unvalidated_change': 1} | None | None |
| py03-missing_none_check | python | deepseek:deepseek-v4-pro | bare | True | True | True | True | True | 0 | COMPLETED | 0.0194 | 48822 | 48822 | 32.6 | {} | None | None |
| py05-wrong_import | python | deepseek:deepseek-v4-pro | bare | True | True | True | True | True | 0 | COMPLETED | 0.0195 | 50858 | 50858 | 20.5 | {} | None | None |
| py07-config_typo | python | deepseek:deepseek-v4-pro | bare | True | True | True | True | True | 0 | COMPLETED | 0.015 | 44062 | 44062 | 18.5 | {} | None | None |
| py10-wrong_operator | python | deepseek:deepseek-v4-pro | bare | True | True | True | True | True | 0 | COMPLETED | 0.0118 | 27539 | 27539 | 14.3 | {} | None | None |
| ts01-off_by_one | typescript | deepseek:deepseek-v4-pro | bare | True | True | True | True | True | 0 | COMPLETED | 0.0164 | 42420 | 42420 | 22.9 | {} | None | None |
| ts02-wrong_operator | typescript | deepseek:deepseek-v4-pro | bare | True | True | True | True | True | 0 | COMPLETED | 0.0152 | 37051 | 37051 | 19.3 | {} | None | None |
| ts03-missing_none_check | typescript | deepseek:deepseek-v4-pro | bare | True | True | True | True | True | 0 | COMPLETED | 0.0141 | 23964 | 23964 | 17.2 | {} | None | None |
| ts04-async_misuse | typescript | deepseek:deepseek-v4-pro | bare | True | True | True | True | True | 0 | COMPLETED | 0.013 | 29049 | 29049 | 15.2 | {} | None | None |

## 2026-10-08 01:55 UTC — suite deepseek:deepseek-flash@harness

| task | lang | model | profile | located | root_cause | passes | no_collateral | regression_added | interventions | status | cost | tokens_lead | tokens_total | wall_s | guards_fired | verification | stop_reason |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| py01-off_by_one | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0048 | 23312 | 24317 | 13.3 | {} | pass | None |
| py02-wrong_operator | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0048 | 34958 | 35842 | 15.6 | {} | pass | None |
| py03-missing_none_check | python | deepseek:deepseek-flash | harness | True | True | False | True | True | 0 | COMPLETED | 0.0065 | 46977 | 48059 | 22.7 | {} | pass | None |
| py05-wrong_import | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0055 | 39801 | 40716 | 18.3 | {} | pass | None |
| py07-config_typo | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0068 | 66391 | 67414 | 23.7 | {} | pass | None |
| py10-wrong_operator | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0042 | 34261 | 35012 | 13.1 | {} | pass | None |
| ts01-off_by_one | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0054 | 32150 | 33302 | 13.8 | {} | pass | None |
| ts02-wrong_operator | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0057 | 36436 | 37668 | 16.2 | {} | pass | None |
| ts03-missing_none_check | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0109 | 48235 | 50691 | 34.6 | {} | pass | None |
| ts04-async_misuse | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.008 | 65411 | 66813 | 28.0 | {} | pass | None |
