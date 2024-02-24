from airflow import DAG
from datetime import timedelta
from airflow.operators.empty import EmptyOperator

project_name = 'price_prediction_train_models_interval_1w'

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
    project_name,
    default_args=default_args,
    schedule_interval='*/15 * * * *'
)

start = EmptyOperator(
    dag=main_dag,
    task_id="train_models_interval_1w_task"
)

DAGS = [main_dag]
