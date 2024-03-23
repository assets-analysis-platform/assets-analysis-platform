"""
extract_ur_data_etl_job.py
~~~~~~~~~~

Usage:
    $SPARK_HOME/bin/spark-submit \
    --master spark://localhost:7077 \
    --py-files packages.zip \
    --files configs/extract_ur_data_etl_config.json \
    jobs/extract_ur_data_etl_job.py <eth_transactions.csv> <eth_logs.csv> <output_path>
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv
from eth_abi.exceptions import InsufficientDataBytes
from web3 import Web3
from redis import StrictRedis
from hexbytes import HexBytes
from dependencies.spark import start_spark
from uniswap_universal_router_decoder import RouterCodec
from pyspark.sql import SparkSession, DataFrame, Row
from pyspark.sql.functions import col, split
from web3.exceptions import ABIFunctionNotFound, NoABIFunctionsFound, NoABIEventsFound

load_dotenv()


def main(eth_transactions_csv_uri: str,
         eth_logs_csv_uri: str,
         output_uri: str) -> None:
    """Spark Uniswap Universal Router transactions ETL script.

    Parameters:
    eth_transactions_csv_uri: str
        Full path to CSV file containing Ethereum blockchain transactions.
    eth_logs_csv_uri: str
        Full path to CSV file containing Ethereum blockchain logs.
    output_uri: str
        The output path for the extracted data. Result will be saved as .csv file.

    :return: None
    """
    spark, LOG, config = start_spark(
        app_name='Identify Uniswap Universal Router transactions',
        files=['configs/uniswap-exchange/extract-ur-transactions-pipeline/extract_ur_data_etl_job_config.json'],
        spark_config={
            'spark.hadoop.fs.s3a.aws.credentials.provider': 'org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider',
            'spark.hadoop.fs.s3a.access.key': os.getenv('AWS_ACCESS_KEY_ID'),
            'spark.hadoop.fs.s3a.secret.key': os.getenv('AWS_SECRET_ACCESS_KEY'),
            'spark.hadoop.fs.s3a.endpoint': f"{os.getenv('AWS_DEFAULT_REGION')}.amazonaws.com",
            'spark.hadoop.fs.s3.impl': 'org.apache.hadoop.fs.s3a.S3AFileSystem',
            'spark.sql.shuffle.partitions': 30,
            'spark.sql.files.maxPartitionBytes': 134217728,  # 128 mb
        }
    )

    LOG.warn('Identify Uniswap Universal Router transactions job is up and running')

    # ETL
    eth_blockchain_transactions_df = get_data(spark, eth_transactions_csv_uri)
    eth_blockchain_logs_df = get_data(spark, eth_logs_csv_uri)
    result = retrieve_ur_transactions(transactions_df=eth_blockchain_transactions_df, logs_df=eth_blockchain_logs_df)
    write_to_s3(result, output_uri)

    LOG.warn('Identify Uniswap Universal Router transactions job success')
    spark.stop()
    return None


def _get_abi_from_etherscan(contract_address: str, api_key: str) -> (str, list):
    """Get contract ABI from Etherscan API

    Retrieves ABI for given contract address from Etherscan.io API

    Parameters:
        contract_address: str
            Contract address to get ABI for
        api_key: str
            Etherscan.io API secret key

    :return: (str, list)
    """
    etherscan_uri = f"https://api.etherscan.io/api?module=contract&action=getabi&address={contract_address}&apikey={api_key}"
    try:
        resp = requests.get(url=etherscan_uri).json()
        resp_status = int(resp.get("status"))
        resp_result = resp.get("result")
        if resp_status == 1:
            return resp_status, resp_result
        else:
            return resp_status, []
    except (json.decoder.JSONDecodeError, requests.exceptions.JSONDecodeError):
        return '0', []


def _get_abi(redis_client: StrictRedis, contract_address: str, api_key: str) -> list:
    """Get contract ABI

    Retrieves ABI for given contract address from Ethereum blockchain network.

    1. Check if ABI is present in redis cache
      - if yes, get it and return (END)
      - if no, call _get_abi_from_etherscan (point 2)
    2. Get ABI from Etherscan API
      - call API
      - save result to Redis cache
      - return ABI for further processing (END)

    Parameters:
        redis_client: StrictRedis
            Redis client instance
        contract_address: str
            Address for which ABI is to be retrieved
        api_key: str
            Etherscan.io API secret key

    :return: list
    """
    key_prefix = 'contract_'
    key_name = f'{key_prefix}{contract_address}'

    if redis_client.exists(key_name):
        return json.loads(redis_client.get(key_name).decode('utf-8'))
    else:
        resp_status, result = _get_abi_from_etherscan(contract_address, api_key)
        if resp_status == 1:  # 1 means HTTP 200 OK
            redis_client.set(key_name, json.dumps(result))
            return result
        return []


def _get_token_name(redis_client: StrictRedis, web3: Web3, etherscan_api_key: str, token_address: any) -> (str, str):
    """Get token name.

    Retrieves token name and symbol for given token address.

    Checks if token address is present in redis cache, if yes, then retrieves the values from it,
    otherwise the values will be retrieved using web3 library.

    Parameters:
        redis_client: StrictRedis
            Redis client instance
        web3: Web3
            Web3 library instance
        etherscan_api_key: str
            Etherscan.io API secret key
        token_address: Any
            Token address.

    :return: (str, str)
    """

    key_token_name_prefix = 'token_name_'
    key_token_name = f'{key_token_name_prefix}{token_address}'
    key_token_symbol_prefix = 'token_symbol_'
    key_token_symbol_name = f'{key_token_symbol_prefix}{token_address}'

    if redis_client.exists(key_token_name) and redis_client.exists(key_token_symbol_name):
        return redis_client.get(key_token_name).decode('utf-8'), redis_client.get(key_token_symbol_name).decode('utf-8')
    else:
        contract = web3.eth.contract(address=token_address, abi=_get_abi(redis_client, token_address, etherscan_api_key))
        try:
            name_result, symbol_result = contract.functions.name().call(), contract.functions.symbol().call()
            redis_client.set(key_token_name, name_result)
            redis_client.set(key_token_symbol_name, symbol_result)
            return name_result, symbol_result
        except (ABIFunctionNotFound, NoABIFunctionsFound):
            # impl_contract = contract_from.functions.implementation.call()
            # row_dict['token_in_name'] = impl_contract.functions.name().call()
            # row_dict['token_in_symbol'] = impl_contract.functions.symbol().call()
            # TODO -> check if it's possible to identify proxy contracts
            return 'ABIFunctionNotFound_proxy_contract', 'ABIFunctionNotFound_proxy_contract'


def _parse_row(redis_client, web3, router_codec, etherscan_api_key, row: Row):

    # UR transactions tracked commands
    CMD_V2_SWAP_EXACT_IN = 'V2_SWAP_EXACT_IN'
    CMD_V2_SWAP_EXACT_OUT = 'V2_SWAP_EXACT_OUT'
    CMD_V3_SWAP_EXACT_IN = 'V3_SWAP_EXACT_IN'
    CMD_V3_SWAP_EXACT_OUT = 'V3_SWAP_EXACT_OUT'
    tracked_cmds = (CMD_V2_SWAP_EXACT_IN, CMD_V2_SWAP_EXACT_OUT, CMD_V3_SWAP_EXACT_IN, CMD_V3_SWAP_EXACT_OUT)

    row_dict = row.asDict()

    try:
        decoded_trx_input = router_codec.decode.function_input(row_dict['input'])

        filtered_data = [item for item in decoded_trx_input[1]['inputs'] if
                         any(cmd_name in str(item[0]) for cmd_name in tracked_cmds)]

        function, params = filtered_data[0]

        cmd = str(function)

        if CMD_V2_SWAP_EXACT_IN in cmd:
            row_dict['command_identifier'] = CMD_V2_SWAP_EXACT_IN
            row_dict['token_address_in'] = params['path'][0]
            row_dict['token_address_out'] = params['path'][1]
            row_dict['swap_amount_in'] = params['amountIn']
            row_dict['swap_amount_in_max'] = None
            row_dict['swap_amount_out'] = None
            row_dict['swap_amount_out_min'] = params['amountOutMin']
        elif CMD_V2_SWAP_EXACT_OUT in cmd:
            row_dict['command_identifier'] = CMD_V2_SWAP_EXACT_OUT
            row_dict['token_address_in'] = params['path'][0]
            row_dict['token_address_out'] = params['path'][1]
            row_dict['swap_amount_in'] = None
            row_dict['swap_amount_in_max'] = params['amountInMax']
            row_dict['swap_amount_out'] = params['amountOut']
            row_dict['swap_amount_out_min'] = None
        elif CMD_V3_SWAP_EXACT_IN in cmd:
            decoded_path = router_codec.decode.v3_path(CMD_V3_SWAP_EXACT_IN, params['path'])
            row_dict['command_identifier'] = CMD_V3_SWAP_EXACT_IN
            row_dict['token_address_in'] = decoded_path[0]
            row_dict['token_address_out'] = decoded_path[2]
            row_dict['swap_amount_in'] = params['amountIn']
            row_dict['swap_amount_in_max'] = None
            row_dict['swap_amount_out'] = None
            row_dict['swap_amount_out_min'] = params['amountOutMin']
        elif CMD_V3_SWAP_EXACT_OUT in cmd:
            decoded_path = router_codec.decode.v3_path(CMD_V3_SWAP_EXACT_OUT, params['path'])
            row_dict['command_identifier'] = CMD_V3_SWAP_EXACT_OUT
            row_dict['token_address_in'] = decoded_path[2]
            row_dict['token_address_out'] = decoded_path[0]
            row_dict['swap_amount_in'] = None
            row_dict['swap_amount_in_max'] = params['amountInMax']
            row_dict['swap_amount_out'] = params['amountOut']
            row_dict['swap_amount_out_min'] = None

        row_dict['token_in_name'], row_dict['token_in_symbol'] = _get_token_name(redis_client, web3, etherscan_api_key, row_dict['token_address_in'])
        row_dict['token_out_name'], row_dict['token_out_symbol'] = _get_token_name(redis_client, web3, etherscan_api_key, row_dict['token_address_out'])

        address = web3.to_checksum_address(row_dict['event_src_addr'])
        address_abi = _get_abi(redis_client, address, etherscan_api_key)
        pool_contract = web3.eth.contract(address=address, abi=address_abi)
        cmd_identifier = row_dict['command_identifier']
        try:
            decoded_event = pool_contract.events.Swap().process_log({
                'data': row_dict['data'],
                'topics': [HexBytes(topic) for topic in row_dict['topics'].split(",")],
                'logIndex': row_dict['log_index'],
                'transactionIndex': row_dict['transaction_index'],
                'transactionHash': row_dict['transaction_hash'],
                'address': row_dict['event_src_addr'],
                'blockHash': row_dict['block_hash'],
                'blockNumber': row_dict['block_number']
            })

            if cmd_identifier in (CMD_V2_SWAP_EXACT_IN, CMD_V2_SWAP_EXACT_OUT):
                try:
                    row_dict['v2_amount0In'] = decoded_event['args']['amount0In']
                except KeyError:
                    row_dict['v2_amount0In'] = None
                try:
                    row_dict['v2_amount1In'] = decoded_event['args']['amount1In']
                except KeyError:
                    row_dict['v2_amount1In'] = None
                try:
                    row_dict['v2_amount0Out'] = decoded_event['args']['amount0Out']
                except KeyError:
                    row_dict['v2_amount0Out'] = None
                try:
                    row_dict['v2_amount1Out'] = decoded_event['args']['amount1Out']
                except KeyError:
                    row_dict['v2_amount1Out'] = None
                row_dict['v3_amount0'] = None
                row_dict['v3_amount1'] = None
                row_dict['v3_sqrtPriceX96'] = None
                row_dict['v3_liquidity'] = None
                row_dict['v3_tick'] = None
            elif cmd_identifier in (CMD_V3_SWAP_EXACT_IN, CMD_V3_SWAP_EXACT_OUT):
                row_dict['v2_amount0In'] = None
                row_dict['v2_amount1In'] = None
                row_dict['v2_amount0Out'] = None
                row_dict['v2_amount1Out'] = None
                try:
                    row_dict['v3_amount0'] = decoded_event['args']['amount0']
                except KeyError:
                    row_dict['v3_amount0'] = None
                try:
                    row_dict['v3_amount1'] = decoded_event['args']['amount1']
                except KeyError:
                    row_dict['v3_amount1'] = None
                try:
                    row_dict['v3_sqrtPriceX96'] = decoded_event['args']['sqrtPriceX96']
                except KeyError:
                    row_dict['v3_sqrtPriceX96'] = None
                try:
                    row_dict['v3_liquidity'] = decoded_event['args']['liquidity']
                except KeyError:
                    row_dict['v3_liquidity'] = None
                try:
                    row_dict['v3_tick'] = decoded_event['args']['tick']
                except KeyError:
                    row_dict['v3_tick'] = None
        except NoABIEventsFound:
            if cmd_identifier in (CMD_V2_SWAP_EXACT_IN, CMD_V2_SWAP_EXACT_OUT):
                row_dict['v2_amount0In'] = 'proxy_contract_NoABIEventsFound'
                row_dict['v2_amount1In'] = 'proxy_contract_NoABIEventsFound'
                row_dict['v2_amount0Out'] = 'proxy_contract_NoABIEventsFound'
                row_dict['v2_amount1Out'] = 'proxy_contract_NoABIEventsFound'
                row_dict['v3_amount0'] = None
                row_dict['v3_amount1'] = None
                row_dict['v3_sqrtPriceX96'] = None
                row_dict['v3_liquidity'] = None
                row_dict['v3_tick'] = None
            elif cmd_identifier in (CMD_V3_SWAP_EXACT_IN, CMD_V3_SWAP_EXACT_OUT):
                row_dict['v2_amount0In'] = None
                row_dict['v2_amount1In'] = None
                row_dict['v2_amount0Out'] = None
                row_dict['v2_amount1Out'] = None
                row_dict['v3_amount0'] = 'proxy_contract_NoABIEventsFound'
                row_dict['v3_amount1'] = 'proxy_contract_NoABIEventsFound'
                row_dict['v3_sqrtPriceX96'] = 'proxy_contract_NoABIEventsFound'
                row_dict['v3_liquidity'] = 'proxy_contract_NoABIEventsFound'
                row_dict['v3_tick'] = 'proxy_contract_NoABIEventsFound'

    except InsufficientDataBytes:
        row_dict['command_identifier'] = 'UNKNOWN'
        row_dict['token_address_in'] = None
        row_dict['token_address_out'] = None
        row_dict['swap_amount_in'] = None
        row_dict['swap_amount_in_max'] = None
        row_dict['swap_amount_out'] = None
        row_dict['swap_amount_out_min'] = None
        row_dict['token_in_name'] = None
        row_dict['token_in_symbol'] = None
        row_dict['token_out_name'] = None
        row_dict['token_out_symbol'] = None
        row_dict['v2_amount0In'] = None
        row_dict['v2_amount1In'] = None
        row_dict['v2_amount0Out'] = None
        row_dict['v2_amount1Out'] = None
        row_dict['v3_amount0'] = None
        row_dict['v3_amount1'] = None
        row_dict['v3_sqrtPriceX96'] = None
        row_dict['v3_liquidity'] = None
        row_dict['v3_tick'] = None

    return Row(**row_dict)


def _parse_partition(iterator):
    redis_client = StrictRedis(host=os.getenv("REDIS_HOST"), port=int(os.getenv("REDIS_PORT")), db=int(os.getenv("REDIS_DB")))
    rpc_provider = Web3.HTTPProvider(os.getenv('RPC_INFURA_HTTPS_ENDPOINT'))
    web3 = Web3(provider=rpc_provider)
    router_codec = RouterCodec(w3=web3)
    etherscan_api_key = os.getenv("ETHERSCAN_API_KEY")

    for row in iterator:
        yield _parse_row(redis_client, web3, router_codec, etherscan_api_key, row)

    redis_client.close()


def get_data(spark: SparkSession, uri: str) -> DataFrame:
    return spark.read.option('header', True).csv(uri)


def retrieve_ur_transactions(transactions_df: DataFrame, logs_df: DataFrame) -> DataFrame:

    # Universal Router contract address
    UNIVERSAL_ROUTER_CONTRACT_ADDRESS = ["0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD", "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad"]

    # UR tracked events
    UNISWAP_V2_SWAP_EVENT = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
    UNISWAP_V3_SWAP_EVENT = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
    TRACKED_EVENTS = [UNISWAP_V2_SWAP_EVENT, UNISWAP_V3_SWAP_EVENT]

    eth_blockchain_transactions_df_columns = ['hash', 'from_address', 'to_address', 'value', 'gas', 'gas_price',
                                              'input', 'block_timestamp', 'max_fee_per_gas', 'max_priority_fee_per_gas',
                                              'transaction_type']
    eth_blockchain_logs_df_columns = ['log_index', 'transaction_hash', 'transaction_index', 'block_hash',
                                      'block_number', 'address', 'data', 'topics']

    eth_blockchain_transactions_df = (transactions_df
                                      .select(*[col(column) for column in eth_blockchain_transactions_df_columns])
                                      .filter(col('to_address').isin(UNIVERSAL_ROUTER_CONTRACT_ADDRESS) & ~(col('input') == '0x'))
                                      .withColumnRenamed('hash', 'transaction_hash')
                                      .withColumnRenamed('from_address', 'sender_address'))

    eth_blockchain_logs_df = (logs_df
                              .select(*[col(column) for column in eth_blockchain_logs_df_columns])
                              .withColumn('topic', split(logs_df['topics'], ",").getItem(0))
                              .filter(col('topic').isin(TRACKED_EVENTS))
                              .withColumnRenamed('address', 'event_src_addr'))

    result = (eth_blockchain_transactions_df
              .join(eth_blockchain_logs_df, "transaction_hash")
              .cache()
              .rdd.mapPartitions(_parse_partition)
              .toDF())

    return result


def write_to_s3(df: DataFrame, s3_result_uri: str) -> None:
    (df
     .coalesce(3)
     .write
     .mode("overwrite")
     .option("header", True)
     .csv(s3_result_uri))


if __name__ == "__main__":
    main(
        eth_transactions_csv_uri=sys.argv[1],
        eth_logs_csv_uri=sys.argv[2],
        output_uri=sys.argv[3]
    )
