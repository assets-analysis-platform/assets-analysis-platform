# Uniswap Universal Router contract transactions
## commands

```
airflow dags trigger 'uniswap_exchange_extract_ur_transactions' -r 'test-run-1' --conf '{"asset_name": "^RUI", "start_date":"2022-08-06", "end_date":"2022-08-06", "interval": "1h"}'
```

Example Configuration JSON (must be a dict object) for Airflow UI
```
{
    "asset_name": "^RUI",
    "start_date": "2022-08-06",
    "end_date": "2022-08-06",
    "interval": "1h"
}
```