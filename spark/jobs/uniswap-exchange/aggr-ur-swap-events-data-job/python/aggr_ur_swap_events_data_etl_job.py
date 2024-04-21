"""
aggr_ur_swap_events_data_etl_job.py
~~~~~~~~~~

Sample usage:
    $SPARK_HOME/bin/spark-submit \
    --master spark://localhost:7077 \
    --py-files packages.zip \
    --files configs/uniswap-exchange/aggr-ur-swap-events-data-job/aggr_ur_data_etl_config.json \
    jobs/uniswap-exchange/aggr-ur-swap-events-data-job/python/aggr_ur_swap_events_data_etl_job.py <input_csv_uri_1,input_csv_uri_2,input_csv_uri_3,input_csv_uri_xx> <output_uri_for_csv_with_success_rows> <output_uri_for_csv_with_failed_rows>
"""

import os
import sys
from dependencies.spark import start_spark
from pyspark.sql import SparkSession, DataFrame
from pyspark.sql.functions import col, udf
from pyspark.sql.types import StringType


def main(input_csv_files_uris: str, output_uri: str, failed_rows_output_uri: str) -> None:
    """Spark Uniswap Universal Router swap events aggr ETL script.

    This pyspark job aggregates Uniswap UR swap events from all provided .csv input files.

    Parameters:
    input_csv_files: str
        Comma separated paths to input CSV files with UR swap events.
    output_uri: str
        The output path for the extracted data. Result will be saved as .csv file.
    failed_rows_output_uri: str
        The output path for .csv file containing records for which data processing failed.

    Returns: None
    """
    spark, LOG, config = start_spark(
        app_name='Aggregate Uniswap Universal Router swaps',
        spark_config={
            'spark.hadoop.fs.s3a.aws.credentials.provider': 'org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider',
            'spark.hadoop.fs.s3a.access.key': os.getenv('AWS_ACCESS_KEY_ID'),
            'spark.hadoop.fs.s3a.secret.key': os.getenv('AWS_SECRET_ACCESS_KEY'),
            'spark.hadoop.fs.s3a.endpoint': f"{os.getenv('AWS_DEFAULT_REGION')}.amazonaws.com",
            'spark.hadoop.fs.s3.impl': 'org.apache.hadoop.fs.s3a.S3AFileSystem',
        }
    )

    LOG.warn('Aggregate Uniswap Universal Router swaps job is up and running')

    # ETL
    data_df = get_data(spark, input_csv_files_uris)
    success_rows_df,  failed_rows_df = process_data(df=data_df)
    write_to_s3(success_rows_df, output_uri)
    write_to_s3(failed_rows_df, failed_rows_output_uri)

    LOG.warn('Aggregate Uniswap Universal Router swaps job SUCCESS')
    spark.stop()
    return None


@udf(returnType=StringType())
def udf_get_value_in(event_type, v2_amount0In, v2_amount1In, v3_amount0, v3_amount1):
    if event_type in ['V2_SWAP_EXACT_IN', 'V2_SWAP_EXACT_OUT']:
        if int(v2_amount0In) > 0:
            return v2_amount0In
        else:
            return v2_amount1In
    elif event_type in ['V3_SWAP_EXACT_IN', 'V3_SWAP_EXACT_OUT']:
        if int(v3_amount0) >= 0:
            return v3_amount0
        else:
            return v3_amount1


@udf(returnType=StringType())
def udf_get_value_out(event_type, v2_amount0In, v2_amount1Out, v2_amount0Out, v3_amount0, v3_amount1):
    if event_type in ['V2_SWAP_EXACT_IN', 'V2_SWAP_EXACT_OUT']:
        if int(v2_amount0In) > 0:
            return v2_amount1Out
        else:
            return v2_amount0Out
    elif event_type in ['V3_SWAP_EXACT_IN', 'V3_SWAP_EXACT_OUT']:
        if int(v3_amount0) >= 0:
            return abs(int(v3_amount1))
        else:
            return abs(int(v3_amount0))


def get_data(spark: SparkSession, input_csv_files: str) -> DataFrame:
    paths_list = input_csv_files.split(',')
    return spark.read.option('header', True).csv(paths_list)


def process_data(df: DataFrame) -> (DataFrame, DataFrame):

    filtered_df = ((df.filter(col('event_type_cmd_identifier')
                              .isin(['V2_SWAP_EXACT_IN', 'V2_SWAP_EXACT_OUT', 'V3_SWAP_EXACT_IN', 'V3_SWAP_EXACT_OUT'])))
                   .cache())

    failed_rows_df = df.subtract(filtered_df)

    success_rows_df = (filtered_df
                       .withColumn('value_in',
                                   udf_get_value_in(
                                       df['event_type_cmd_identifier'],
                                       df['v2_amount0In'],
                                       df['v2_amount1In'],
                                       df['v3_amount0'],
                                       df['v3_amount1']))
                       .withColumn('value_out',
                                   udf_get_value_out(
                                       df['event_type_cmd_identifier'],
                                       df['v2_amount0In'],
                                       df['v2_amount1Out'],
                                       df['v2_amount0Out'],
                                       df['v3_amount0'],
                                       df['v3_amount1']))
                       .select(*[col(column) for column in
                                 ['transaction_hash', 'block_timestamp', 'gas', 'gas_price',
                                  'log_index', 'transaction_swap_number', 'pool_address', 'value_in', 'value_out',
                                  'token_in_name', 'token_in_symbol', 'token_out_name', 'token_out_symbol']]))

    return success_rows_df, failed_rows_df


def write_to_s3(df: DataFrame, s3_result_uri: str) -> None:
    (df
     .repartition(1)
     .write
     .mode("overwrite")
     .option("header", True)
     .csv(s3_result_uri))


if __name__ == "__main__":
    main(
        input_csv_files_uris=sys.argv[1],
        output_uri=sys.argv[2],
        failed_rows_output_uri=sys.argv[3]
    )
