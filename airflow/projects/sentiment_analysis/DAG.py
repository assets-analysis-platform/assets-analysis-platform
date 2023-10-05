from airflow import DAG
from datetime import timedelta
from airflow.operators.empty import EmptyOperator

project_name = 'sentiment_analysis'

default_args = {
    "owner": 'airflow',
    "depends_on_past": False,
    "schedule_interval": None,
    "start_date": "2009-05-13",
    "email": ['testaccount@dummycompany.com'],
    "email_on_failure": True,
    "email_on_retry": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=1)
}

main_dag = DAG(
    project_name,
    default_args=default_args,
    schedule_interval='*/15 * * * *'
)

start = EmptyOperator(
    dag=main_dag,
    task_id=project_name
)

DAGS = [main_dag]
