"""
extract_ur_data_etl_job.py
~~~~~~~~~~

Usage:
    $SPARK_HOME/bin/spark-submit \
    --master spark://localhost:7077 \
    --py-files packages.zip \
    --files configs/extract_ur_data_etl_config.json \
    jobs/extract_ur_data_etl_job.py
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv
from web3 import Web3
from hexbytes import HexBytes
from uniswap_universal_router_decoder import RouterCodec
from pyspark.sql import SparkSession, DataFrame, Row
from pyspark.sql.functions import col, split

load_dotenv()


def main(s3_eth_transactions_uri: str, s3_eth_logs_uri: str, s3_output_uri: str):

    rpc_provider = Web3.HTTPProvider(os.getenv('RPC_INFURA_HTTPS_ENDPOINT'))
    etherscan_api_key = os.getenv("ETHERSCAN_API_KEY")

    web3 = Web3(provider=rpc_provider)
    router_codec = RouterCodec(w3=web3)

    spark = (SparkSession
             .builder
             .appName("Identify Uniswap Universal Router transactions")
             .getOrCreate())

    # ETL
    eth_blockchain_transactions_df = get_data(spark, s3_eth_transactions_uri)
    eth_blockchain_logs_df = get_data(spark, s3_eth_logs_uri)
    result = retrieve_ur_transactions(web3, router_codec, etherscan_api_key, eth_blockchain_transactions_df, eth_blockchain_logs_df)
    write_to_s3(result, s3_output_uri)

    spark.stop()


def _get_abi_from_etherscan(contract_address: str, api_key: str) -> list:
    """
        Get contract ABI from Etherscan API
    """
    etherscan_uri = f"https://api.etherscan.io/api?module=contract&action=getabi&address={contract_address}&apikey={api_key}"
    r = requests.get(url=etherscan_uri)
    return json.loads(json.loads(r.text)['result'])


def _get_abi_from_redis(contract_address: str, key: str) -> list:
    """
        Get contract ABI from Redis cache
    """
    # TODO -> ADD retrieving ABI from Redis cache
    return []


def _get_abi(contract_address: str, api_key: str) -> list:
    """
        Get contract ABI
    """
    # TODO -> ADD retrieving ABI mechanism
    # 1. Check if ABI is present in redis cache
    #       - if yes, get it and return (END)
    #       - if no, call _get_abi_from_etherscan (point 2)
    # 2. Get ABI from Etherscan API
    #       - call API
    #       - save result to Redis cache (asynchronously)
    #       - return value for further processing (END)
    return _get_abi_from_etherscan(contract_address, api_key)   # TODO -> temporary workaround


def _parse_row(web3: Web3, router_codec: RouterCodec, etherscan_api_key: str, row: Row) -> Row:
    # UR transactions tracked commands
    CMD_V2_SWAP_EXACT_IN = 'V2_SWAP_EXACT_IN'
    CMD_V2_SWAP_EXACT_OUT = 'V2_SWAP_EXACT_OUT'
    CMD_V3_SWAP_EXACT_IN = 'V3_SWAP_EXACT_IN'
    CMD_V3_SWAP_EXACT_OUT = 'V3_SWAP_EXACT_OUT'
    tracked_cmds = (CMD_V2_SWAP_EXACT_IN, CMD_V2_SWAP_EXACT_OUT, CMD_V3_SWAP_EXACT_IN, CMD_V3_SWAP_EXACT_OUT)

    row_dict = row.asDict()

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
        row_dict['swap_amount_out_min'] = params['amountOutMin']
    elif CMD_V2_SWAP_EXACT_OUT in cmd:
        row_dict['command_identifier'] = CMD_V2_SWAP_EXACT_OUT
        row_dict['token_address_in'] = params['path'][0]
        row_dict['token_address_out'] = params['path'][1]
        row_dict['swap_amount_in_max'] = params['amountInMax']
        row_dict['swap_amount_out'] = params['amountOut']
    elif CMD_V3_SWAP_EXACT_IN in cmd:
        decoded_path = router_codec.decode.v3_path(CMD_V3_SWAP_EXACT_IN, params['path'])
        row_dict['command_identifier'] = CMD_V3_SWAP_EXACT_IN
        row_dict['token_address_in'] = decoded_path[0]
        row_dict['token_address_out'] = decoded_path[2]
        row_dict['swap_amount_in'] = params['amountIn']
        row_dict['swap_amount_out_min'] = params['amountOutMin']
    elif CMD_V3_SWAP_EXACT_OUT in cmd:
        decoded_path = router_codec.decode.v3_path(CMD_V3_SWAP_EXACT_OUT, params['path'])
        row_dict['command_identifier'] = CMD_V3_SWAP_EXACT_OUT
        row_dict['token_address_in'] = decoded_path[2]
        row_dict['token_address_out'] = decoded_path[0]
        row_dict['swap_amount_in_max'] = params['amountInMax']
        row_dict['swap_amount_out'] = params['amountOut']

    token_address_in = row_dict['token_address_in']
    token_address_out = row_dict['token_address_out']

    contract_from = web3.eth.contract(address=token_address_in, abi=_get_abi(token_address_in, etherscan_api_key))
    contract_to = web3.eth.contract(address=token_address_out, abi=_get_abi(token_address_out, etherscan_api_key))

    row_dict['token_in_name'] = contract_from.functions.name().call()
    row_dict['token_in_symbol'] = contract_from.functions.symbol().call()
    row_dict['token_out_name'] = contract_to.functions.name().call()
    row_dict['token_out_symbol'] = contract_to.functions.symbol().call()

    address = web3.to_checksum_address(row_dict['event_src_addr'])
    address_abi = _get_abi(contract_address=address, api_key=etherscan_api_key)

    pool_contract = web3.eth.contract(address=address, abi=address_abi)

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

    cmd_identifier = row_dict['command_identifier']

    if cmd_identifier in (CMD_V2_SWAP_EXACT_IN, CMD_V2_SWAP_EXACT_OUT):
        row_dict['v2_amount0In'] = decoded_event['args']['amount0In']
        row_dict['v2_amount1In'] = decoded_event['args']['amount1In']
        row_dict['v2_amount0Out'] = decoded_event['args']['amount0Out']
        row_dict['v2_amount1Out'] = decoded_event['args']['amount1Out']
    elif cmd_identifier in (CMD_V3_SWAP_EXACT_IN, CMD_V3_SWAP_EXACT_OUT):
        row_dict['v3_amount0'] = decoded_event['args']['amount0']
        row_dict['v3_amount1'] = decoded_event['args']['amount1']
        row_dict['v3_sqrtPriceX96'] = decoded_event['args']['sqrtPriceX96']
        row_dict['v3_liquidity'] = decoded_event['args']['liquidity']
        row_dict['v3_tick'] = decoded_event['args']['tick']

    return Row(**row_dict)


def get_data(spark: SparkSession, uri: str) -> DataFrame:
    return spark.read.csv(uri)


def retrieve_ur_transactions(web3: Web3, router_codec: RouterCodec, etherscan_api_key: str, transactions_df: DataFrame, logs_df: DataFrame) -> DataFrame:

    # Universal Router contract address
    UNIVERSAL_ROUTER_CONTRACT_ADDRESS = "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD".lower()

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
                                      .filter(transactions_df['to_address'] == UNIVERSAL_ROUTER_CONTRACT_ADDRESS)
                                      .withColumnsRenamed({'hash': 'transaction_hash', 'from_address': 'sender_address'}))
    eth_blockchain_logs_df = (logs_df
                              .select(*[col(column) for column in eth_blockchain_logs_df_columns])
                              .filter(logs_df['transaction_hash'].isin(eth_blockchain_transactions_df['transaction_hash']))
                              .filter(split(logs_df['topics'], ",").getItem(0).isin(TRACKED_EVENTS))
                              .withColumnsRenamed({'address': 'event_src_addr'}))

    merged_df = eth_blockchain_transactions_df.join(eth_blockchain_logs_df, "transaction_hash").cache()

    return merged_df.rdd.map(lambda row: _parse_row(web3, router_codec, etherscan_api_key, row)).toDF()


def write_to_s3(df: DataFrame, s3_result_uri: str) -> None:
    df.write.mode("append").csv(s3_result_uri)


if __name__ == "__main__":
    main(s3_eth_transactions_uri=sys.argv[1], s3_eth_logs_uri=sys.argv[2], s3_output_uri=sys.argv[3])
