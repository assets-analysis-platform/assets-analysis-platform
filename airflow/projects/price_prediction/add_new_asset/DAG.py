from airflow import DAG
from datetime import timedelta
from airflow.operators.empty import EmptyOperator

DAG_name = 'price_prediction_add_new_asset'

default_args = {
    "owner": 'airflow',
    "depends_on_past": False,
    "schedule_interval": None,
    "start_date": "2019-05-13",
    "email": ['testaccount@dummycompany.com'],
    "email_on_failure": True,
    "email_on_retry": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=1)
}

main_dag = DAG(
    DAG_name,
    default_args=default_args,
    schedule_interval=None
)

start = EmptyOperator(
    dag=main_dag,
    task_id="start"
)

DAGS = [main_dag]
