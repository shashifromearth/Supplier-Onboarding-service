# Evaluation report - variant `scan`, extractor `layout`

## KPIs

- **documents**: 13
- **decision_accuracy**: 0.769
- **review_reason_accuracy**: 1.000
- **human_review_rate**: 0.692
- **expected_human_review_rate**: 0.462
- **match_precision (auto-updates to the right supplier)**: 1.000
- **match_recall (true updates automated)**: 0.500
- **false_match_rate (wrong auto-update / auto-updates)**: 0.000
- **duplicate_creation_rate (auto-create of existing supplier)**: 0.000
- **field_accuracy_micro**: 1.000

## Field-level extraction

| field              |   accuracy |   precision |   recall |   n |
|:-------------------|-----------:|------------:|---------:|----:|
| legal_name         |      1.000 |       1.000 |    1.000 |  12 |
| dba_name           |      1.000 |       1.000 |    1.000 |  12 |
| tax_classification |      1.000 |       1.000 |    1.000 |  12 |
| address            |      1.000 |       1.000 |    1.000 |  12 |
| city               |      1.000 |       1.000 |    1.000 |  12 |
| state              |      1.000 |       1.000 |    1.000 |  12 |
| zip                |      1.000 |       1.000 |    1.000 |  12 |
| tin                |      1.000 |       1.000 |    1.000 |  12 |
| tin_type           |      1.000 |       1.000 |    1.000 |  12 |
| signature_present  |      1.000 |       1.000 |    1.000 |  12 |
| signature_date     |      1.000 |       1.000 |    1.000 |  12 |

## Confidence calibration (all non-null predicted fields)

| confidence_bin   |   n |   observed_accuracy |
|:-----------------|----:|--------------------:|
| [0.00,0.70)      |   2 |               1.000 |
| [0.70,0.85)      |   5 |               1.000 |
| [0.85,0.95)      |   9 |               1.000 |
| [0.95,1.00)      | 107 |               1.000 |

## Decision confusion matrix

```
predicted                 CREATE_NEW_SUPPLIER  HUMAN_REVIEW  UPDATE_EXISTING_SUPPLIER
expected                                                                             
CREATE_NEW_SUPPLIER                         2             1                         0
HUMAN_REVIEW                                0             6                         0
UPDATE_EXISTING_SUPPLIER                    0             2                         2
```

## Per-document decisions

| file                           | expected                 | predicted                | predicted_supplier   | review_codes                                     |   confidence | decision_ok   |
|:-------------------------------|:-------------------------|:-------------------------|:---------------------|:-------------------------------------------------|-------------:|:--------------|
| 01_new_supplier_clean.pdf      | CREATE_NEW_SUPPLIER      | HUMAN_REVIEW             | -                    | LOW_CONFIDENCE_CRITICAL_FIELD                    |        0.578 | False         |
| 02_existing_refresh.pdf        | UPDATE_EXISTING_SUPPLIER | UPDATE_EXISTING_SUPPLIER | SUP-1001             |                                                  |        0.862 | True          |
| 03_existing_address_change.pdf | UPDATE_EXISTING_SUPPLIER | HUMAN_REVIEW             | -                    | LOW_CONFIDENCE_CRITICAL_FIELD                    |        0.716 | False         |
| 04_name_match_tin_conflict.pdf | HUMAN_REVIEW             | HUMAN_REVIEW             | -                    | NAME_MATCH_TIN_CONFLICT                          |        0.438 | True          |
| 05_same_tin_different_name.pdf | HUMAN_REVIEW             | HUMAN_REVIEW             | -                    | TIN_NAME_CONFLICT                                |        0.362 | True          |
| 06_missing_signature.pdf       | HUMAN_REVIEW             | HUMAN_REVIEW             | -                    | MISSING_SIGNATURE,LOW_CONFIDENCE_CRITICAL_FIELD  |        0.834 | True          |
| 07_invalid_tin.pdf             | HUMAN_REVIEW             | HUMAN_REVIEW             | -                    | INVALID_EIN_PREFIX,LOW_CONFIDENCE_CRITICAL_FIELD |        0.297 | True          |
| 08_fuzzy_ambiguous.pdf         | HUMAN_REVIEW             | HUMAN_REVIEW             | -                    | FUZZY_MATCH_REVIEW_BAND                          |        0.452 | True          |
| 09_phone_photo_existing.jpg    | UPDATE_EXISTING_SUPPLIER | UPDATE_EXISTING_SUPPLIER | SUP-1006             |                                                  |        0.862 | True          |
| 10_wrong_form_w8ben.pdf        | HUMAN_REVIEW             | HUMAN_REVIEW             | -                    | FORM_TYPE_NOT_W9                                 |        0.000 | True          |
| 11_sole_prop_ssn.png           | CREATE_NEW_SUPPLIER      | CREATE_NEW_SUPPLIER      | -                    |                                                  |        0.918 | True          |
| 12_rev2018_tin_backfill.pdf    | UPDATE_EXISTING_SUPPLIER | HUMAN_REVIEW             | -                    | LOW_CONFIDENCE_CRITICAL_FIELD                    |        0.618 | False         |
| 13_cross_tenant_isolation.pdf  | CREATE_NEW_SUPPLIER      | CREATE_NEW_SUPPLIER      | -                    |                                                  |        0.669 | True          |

## Name matcher: precision/recall vs threshold (eval/name_pairs.csv, n=40)

|   threshold |   precision |   recall |   false_positives |   false_negatives |
|------------:|------------:|---------:|------------------:|------------------:|
|      70.000 |       0.583 |    1.000 |            15.000 |             0.000 |
|      80.000 |       0.700 |    1.000 |             9.000 |             0.000 |
|      85.000 |       0.778 |    1.000 |             6.000 |             0.000 |
|      90.000 |       0.800 |    0.952 |             5.000 |             1.000 |
|      95.000 |       0.952 |    0.952 |             1.000 |             1.000 |

Misclassified pairs at 90:

| name_a                        | name_b                              |   same_entity |   score | note                                      |
|:------------------------------|:------------------------------------|--------------:|--------:|:------------------------------------------|
| Best For You Organics Company | Best 4 You Organics Co              |             1 |  85.000 | digit substitution                        |
| ABC Plumbing                  | ABC Plumbing Supply                 |             0 |  90.000 | subset name - different business          |
| Coho Winery                   | Coho Vineyard & Winery              |             0 |  90.000 | similar brand - different entity          |
| Adventure Works Cycles        | Adventure Works                     |             0 |  90.000 | subset - parent vs subsidiary             |
| Humongous Insurance           | Humongous Insurance Agency of Texas |             0 |  90.000 | subset - agency vs carrier                |
| Gonzalez Landscaping          | Gonzales Landscaping                |             0 |  95.000 | one character - different family business |