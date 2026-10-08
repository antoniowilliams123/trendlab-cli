
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

## 2026-10-08 02:40 UTC — suite 40 deepseek:deepseek-flash@harness

| task | lang | model | profile | located | root_cause | passes | no_collateral | regression_added | interventions | status | cost | tokens_lead | tokens_total | wall_s | guards_fired | verification | stop_reason |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| py01-off_by_one | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0066 | 34974 | 36361 | 22.3 | {} | pass | None |
| py02-wrong_operator | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0038 | 28430 | 29152 | 13.9 | {} | pass | None |
| py03-missing_none_check | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0117 | 77133 | 78980 | 46.7 | {} | pass | None |
| py04-swapped_args | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0081 | 86427 | 87652 | 130.0 | {} | pass | None |
| py05-wrong_import | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0074 | 43465 | 44954 | 129.3 | {} | pass | None |
| py06-early_return | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0042 | 33750 | 34555 | 125.9 | {} | pass | None |
| py07-config_typo | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0057 | 51647 | 52628 | 212.8 | {} | pass | None |
| py08-bad_format | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0053 | 41990 | 43025 | 165.8 | {} | pass | None |
| py09-multi_file_contract | python | deepseek:deepseek-flash | harness | False | False | True | True | True | 0 | COMPLETED | 0.0098 | 46432 | 48436 | 43.4 | {} | pass | None |
| py10-wrong_operator | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0051 | 34285 | 35302 | 31.9 | {} | pass | None |
| py11-off_by_one | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0059 | 35617 | 36770 | 25.7 | {} | pass | None |
| py12-missing_none_check | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0054 | 40865 | 41862 | 40.5 | {} | pass | None |
| py13-swapped_args | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0071 | 33918 | 35404 | 38.4 | {} | pass | None |
| py14-early_return | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0057 | 35751 | 36911 | 23.8 | {} | pass | None |
| py15-wrong_operator | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0045 | 24189 | 25036 | 18.0 | {} | pass | None |
| py16-off_by_one | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0069 | 28354 | 29895 | 24.4 | {} | pass | None |
| py17-wrong_operator | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0083 | 76447 | 77373 | 38.6 | {} | pass | None |
| py18-missing_none_check | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0042 | 28391 | 29259 | 15.7 | {} | pass | None |
| py19-swapped_args | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0118 | 97988 | 99988 | 46.2 | {} | pass | None |
| py20-wrong_import | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0134 | 76690 | 79085 | 50.6 | {} | pass | None |
| py21-early_return | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0074 | 58776 | 59969 | 33.3 | {} | pass | None |
| py22-config_typo | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0135 | 69163 | 71933 | 46.4 | {} | pass | None |
| py23-bad_format | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0117 | 53590 | 55954 | 41.6 | {} | pass | None |
| py24-multi_file_contract | python | deepseek:deepseek-flash | harness | False | False | True | True | True | 0 | COMPLETED | 0.0165 | 59678 | 63422 | 55.0 | {} | pass | None |
| py25-wrong_operator | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0114 | 63657 | 65800 | 40.2 | {} | pass | None |
| py26-off_by_one | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0125 | 54202 | 56639 | 40.5 | {} | pass | None |
| py27-missing_none_check | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0049 | 33633 | 34547 | 15.7 | {} | pass | None |
| py28-swapped_args | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0059 | 48556 | 49539 | 24.3 | {} | pass | None |
| py29-early_return | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0055 | 31053 | 32078 | 17.4 | {} | pass | None |
| py30-wrong_operator | python | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0058 | 44521 | 45433 | 20.3 | {} | pass | None |
| ts01-off_by_one | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0059 | 43411 | 44675 | 18.3 | {} | pass | None |
| ts02-wrong_operator | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0053 | 30652 | 31856 | 15.1 | {} | pass | None |
| ts03-missing_none_check | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0071 | 34221 | 35918 | 21.8 | {} | pass | None |
| ts04-async_misuse | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 1 | COMPLETED | 0.0083 | 65778 | 67303 | 28.7 | {} | pass | None |
| ts05-config_typo | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0061 | 51705 | 52975 | 18.9 | {} | pass | None |
| ts06-wrong_operator | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0062 | 37840 | 39174 | 18.6 | {} | pass | None |
| ts07-early_return | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 1 | COMPLETED | 0.01 | 46085 | 48049 | 32.1 | {} | pass | None |
| ts08-swapped_args | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0066 | 39225 | 40585 | 20.4 | {} | pass | None |
| ts09-bad_format | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0048 | 34427 | 35489 | 14.6 | {} | pass | None |
| ts10-wrong_import | typescript | deepseek:deepseek-flash | harness | True | True | True | True | True | 0 | COMPLETED | 0.0065 | 38056 | 39532 | 22.2 | {} | pass | None |

## 2026-10-08 03:02 UTC — suite 40 deepseek:deepseek-flash@bare

| task | lang | model | profile | located | root_cause | passes | no_collateral | regression_added | interventions | status | cost | tokens_lead | tokens_total | wall_s | guards_fired | verification | stop_reason |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| py01-off_by_one | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0031 | 22126 | 22126 | 8.6 | {} | None | None |
| py02-wrong_operator | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0036 | 42296 | 42296 | 12.4 | {} | None | None |
| py03-missing_none_check | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0087 | 92035 | 92035 | 33.6 | {} | None | None |
| py04-swapped_args | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0048 | 71125 | 71125 | 17.7 | {} | None | None |
| py05-wrong_import | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0038 | 31014 | 31014 | 11.4 | {} | None | None |
| py06-early_return | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0054 | 74224 | 74224 | 20.2 | {} | None | None |
| py07-config_typo | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0048 | 68109 | 68109 | 17.6 | {} | None | None |
| py08-bad_format | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0049 | 52738 | 52738 | 15.5 | {} | None | None |
| py09-multi_file_contract | python | deepseek:deepseek-flash | bare | False | False | True | True | True | 0 | COMPLETED | 0.0056 | 65475 | 65475 | 20.1 | {} | None | None |
| py10-wrong_operator | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0029 | 32079 | 32079 | 9.6 | {} | None | None |
| py11-off_by_one | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0034 | 28741 | 28741 | 10.3 | {} | None | None |
| py12-missing_none_check | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0057 | 84355 | 84355 | 24.2 | {} | None | None |
| py13-swapped_args | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0055 | 71905 | 71905 | 21.2 | {} | None | None |
| py14-early_return | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0047 | 51198 | 51198 | 17.2 | {} | None | None |
| py15-wrong_operator | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0028 | 21415 | 21415 | 7.9 | {} | None | None |
| py16-off_by_one | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.005 | 50527 | 50527 | 17.1 | {} | None | None |
| py17-wrong_operator | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0036 | 34519 | 34519 | 12.2 | {} | None | None |
| py18-missing_none_check | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0046 | 57647 | 57647 | 17.0 | {} | None | None |
| py19-swapped_args | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0057 | 60913 | 60913 | 23.6 | {} | None | None |
| py20-wrong_import | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.008 | 105784 | 105784 | 27.8 | {} | None | None |
| py21-early_return | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 1 | COMPLETED | 0.0091 | 123665 | 123665 | 33.3 | {} | None | None |
| py22-config_typo | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0078 | 109329 | 109329 | 28.2 | {} | None | None |
| py23-bad_format | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 1 | COMPLETED | 0.0142 | 214477 | 214477 | 658.6 | {} | None | None |
| py24-multi_file_contract | python | deepseek:deepseek-flash | bare | False | False | True | True | True | 0 | COMPLETED | 0.0039 | 37257 | 37257 | 12.1 | {} | None | None |
| py25-wrong_operator | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0048 | 60962 | 60962 | 19.5 | {} | None | None |
| py26-off_by_one | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0048 | 57968 | 57968 | 17.1 | {} | None | None |
| py27-missing_none_check | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0051 | 80742 | 80742 | 22.9 | {} | None | None |
| py28-swapped_args | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0041 | 40693 | 40693 | 14.0 | {} | None | None |
| py29-early_return | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0044 | 50256 | 50256 | 15.1 | {} | None | None |
| py30-wrong_operator | python | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0058 | 62990 | 62990 | 20.4 | {} | None | None |
| ts01-off_by_one | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0035 | 29729 | 29729 | 11.0 | {} | None | None |
| ts02-wrong_operator | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0037 | 35558 | 35558 | 11.7 | {} | None | None |
| ts03-missing_none_check | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0039 | 35364 | 35364 | 13.2 | {} | None | None |
| ts04-async_misuse | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0052 | 65286 | 65286 | 19.3 | {} | None | None |
| ts05-config_typo | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0047 | 46433 | 46433 | 13.5 | {} | None | None |
| ts06-wrong_operator | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.004 | 37387 | 37387 | 13.3 | {} | None | None |
| ts07-early_return | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 1 | COMPLETED | 0.006 | 69518 | 69518 | 22.7 | {} | None | None |
| ts08-swapped_args | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0049 | 75140 | 75140 | 18.9 | {} | None | None |
| ts09-bad_format | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.0033 | 28660 | 28660 | 10.3 | {} | None | None |
| ts10-wrong_import | typescript | deepseek:deepseek-flash | bare | True | True | True | True | True | 0 | COMPLETED | 0.004 | 43694 | 43694 | 13.5 | {} | None | None |
