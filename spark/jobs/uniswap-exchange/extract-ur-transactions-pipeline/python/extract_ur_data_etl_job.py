"""
extract_ur_data_etl_job.py
~~~~~~~~~~

Example usage:
    $SPARK_HOME/bin/spark-submit \
    --master spark://localhost:7077 \
    --py-files packages.zip \
    --files configs/uniswap-exchange/extract-ur-transactions-pipeline/extract_ur_data_etl_config.json \
    jobs/uniswap-exchange/extract-ur-transactions-pipeline/python/extract_ur_data_etl_job.py <eth_transactions.csv> <eth_logs.csv> <output_path>
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv
from eth_abi.exceptions import InsufficientDataBytes
from pyspark.sql.types import StructField, StringType, StructType
from web3 import Web3
from redis import StrictRedis
from hexbytes import HexBytes
from dependencies.spark import start_spark
from uniswap_universal_router_decoder import RouterCodec
from pyspark.sql import SparkSession, DataFrame, Row
from pyspark.sql.window import Window
from pyspark.sql.functions import col, split, row_number
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

    Returns: None
    """
    spark, LOG, config = start_spark(
        app_name='Identify Uniswap Universal Router transactions swaps',
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

    LOG.warn('Identify Uniswap Universal Router transactions swaps job is up and running')

    # ETL
    eth_blockchain_transactions_df = get_data(spark, eth_transactions_csv_uri)
    eth_blockchain_logs_df = get_data(spark, eth_logs_csv_uri)
    result = retrieve_ur_transactions(transactions_df=eth_blockchain_transactions_df, logs_df=eth_blockchain_logs_df)
    write_to_s3(result, output_uri)

    LOG.warn('Identify Uniswap Universal Router transactions swaps job SUCCESS')
    spark.stop()
    return None


def _get_abi_from_etherscan(contract_address: str, api_key: str) -> (int, list):
    """Get contract ABI from Etherscan.io API

    Retrieves ABI for given contract address from Etherscan.io Ethereum Blockchain Explorer.

    Parameters:
        contract_address: str
            Contract address to get ABI for
        api_key: str
            Etherscan.io API secret key

    Returns:
        tuple: A tuple containing multiple values.
            - element 1 (int): HTTP Response status
            - element 2 (list): HTTP Response result
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
    except (json.decoder.JSONDecodeError,
            requests.exceptions.JSONDecodeError,
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError):
        return 0, []


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

    Returns:
        list: A list containing ABI.
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


def _get_uniswap_v2_default_abi() -> str:
    """Get default Uniswap V2 ABI

    Returns default uni v2 token ABI.

    Returns:
        str: A string containing ABI.
    """
    return '[{"inputs":[],"payable":false,"stateMutability":"nonpayable","type":"constructor"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"owner","type":"address"},{"indexed":true,"internalType":"address","name":"spender","type":"address"},{"indexed":false,"internalType":"uint256","name":"value","type":"uint256"}],"name":"Approval","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"sender","type":"address"},{"indexed":false,"internalType":"uint256","name":"amount0","type":"uint256"},{"indexed":false,"internalType":"uint256","name":"amount1","type":"uint256"},{"indexed":true,"internalType":"address","name":"to","type":"address"}],"name":"Burn","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"sender","type":"address"},{"indexed":false,"internalType":"uint256","name":"amount0","type":"uint256"},{"indexed":false,"internalType":"uint256","name":"amount1","type":"uint256"}],"name":"Mint","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"sender","type":"address"},{"indexed":false,"internalType":"uint256","name":"amount0In","type":"uint256"},{"indexed":false,"internalType":"uint256","name":"amount1In","type":"uint256"},{"indexed":false,"internalType":"uint256","name":"amount0Out","type":"uint256"},{"indexed":false,"internalType":"uint256","name":"amount1Out","type":"uint256"},{"indexed":true,"internalType":"address","name":"to","type":"address"}],"name":"Swap","type":"event"},{"anonymous":false,"inputs":[{"indexed":false,"internalType":"uint112","name":"reserve0","type":"uint112"},{"indexed":false,"internalType":"uint112","name":"reserve1","type":"uint112"}],"name":"Sync","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"from","type":"address"},{"indexed":true,"internalType":"address","name":"to","type":"address"},{"indexed":false,"internalType":"uint256","name":"value","type":"uint256"}],"name":"Transfer","type":"event"},{"constant":true,"inputs":[],"name":"DOMAIN_SEPARATOR","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"MINIMUM_LIQUIDITY","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"PERMIT_TYPEHASH","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":true,"inputs":[{"internalType":"address","name":"","type":"address"},{"internalType":"address","name":"","type":"address"}],"name":"allowance","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"value","type":"uint256"}],"name":"approve","outputs":[{"internalType":"bool","name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"internalType":"address","name":"to","type":"address"}],"name":"burn","outputs":[{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"internalType":"uint8","name":"","type":"uint8"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"factory","outputs":[{"internalType":"address","name":"","type":"address"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"getReserves","outputs":[{"internalType":"uint112","name":"_reserve0","type":"uint112"},{"internalType":"uint112","name":"_reserve1","type":"uint112"},{"internalType":"uint32","name":"_blockTimestampLast","type":"uint32"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"internalType":"address","name":"_token0","type":"address"},{"internalType":"address","name":"_token1","type":"address"}],"name":"initialize","outputs":[],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"kLast","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"internalType":"address","name":"to","type":"address"}],"name":"mint","outputs":[{"internalType":"uint256","name":"liquidity","type":"uint256"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"name","outputs":[{"internalType":"string","name":"","type":"string"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":true,"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"nonces","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"internalType":"address","name":"owner","type":"address"},{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"value","type":"uint256"},{"internalType":"uint256","name":"deadline","type":"uint256"},{"internalType":"uint8","name":"v","type":"uint8"},{"internalType":"bytes32","name":"r","type":"bytes32"},{"internalType":"bytes32","name":"s","type":"bytes32"}],"name":"permit","outputs":[],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"price0CumulativeLast","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"price1CumulativeLast","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"internalType":"address","name":"to","type":"address"}],"name":"skim","outputs":[],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":false,"inputs":[{"internalType":"uint256","name":"amount0Out","type":"uint256"},{"internalType":"uint256","name":"amount1Out","type":"uint256"},{"internalType":"address","name":"to","type":"address"},{"internalType":"bytes","name":"data","type":"bytes"}],"name":"swap","outputs":[],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"symbol","outputs":[{"internalType":"string","name":"","type":"string"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[],"name":"sync","outputs":[],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"totalSupply","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"value","type":"uint256"}],"name":"transfer","outputs":[{"internalType":"bool","name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":false,"inputs":[{"internalType":"address","name":"from","type":"address"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"value","type":"uint256"}],"name":"transferFrom","outputs":[{"internalType":"bool","name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"}]'


def _get_uniswap_v3_default_abi() -> str:
    """Get default Uniswap V3 ABI

    Returns default uni v3 token ABI.

    Returns:
        str: A string containing ABI.
    """
    return '[{"inputs":[],"stateMutability":"nonpayable","type":"constructor"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"owner","type":"address"},{"indexed":true,"internalType":"int24","name":"tickLower","type":"int24"},{"indexed":true,"internalType":"int24","name":"tickUpper","type":"int24"},{"indexed":false,"internalType":"uint128","name":"amount","type":"uint128"},{"indexed":false,"internalType":"uint256","name":"amount0","type":"uint256"},{"indexed":false,"internalType":"uint256","name":"amount1","type":"uint256"}],"name":"Burn","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"owner","type":"address"},{"indexed":false,"internalType":"address","name":"recipient","type":"address"},{"indexed":true,"internalType":"int24","name":"tickLower","type":"int24"},{"indexed":true,"internalType":"int24","name":"tickUpper","type":"int24"},{"indexed":false,"internalType":"uint128","name":"amount0","type":"uint128"},{"indexed":false,"internalType":"uint128","name":"amount1","type":"uint128"}],"name":"Collect","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"sender","type":"address"},{"indexed":true,"internalType":"address","name":"recipient","type":"address"},{"indexed":false,"internalType":"uint128","name":"amount0","type":"uint128"},{"indexed":false,"internalType":"uint128","name":"amount1","type":"uint128"}],"name":"CollectProtocol","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"sender","type":"address"},{"indexed":true,"internalType":"address","name":"recipient","type":"address"},{"indexed":false,"internalType":"uint256","name":"amount0","type":"uint256"},{"indexed":false,"internalType":"uint256","name":"amount1","type":"uint256"},{"indexed":false,"internalType":"uint256","name":"paid0","type":"uint256"},{"indexed":false,"internalType":"uint256","name":"paid1","type":"uint256"}],"name":"Flash","type":"event"},{"anonymous":false,"inputs":[{"indexed":false,"internalType":"uint16","name":"observationCardinalityNextOld","type":"uint16"},{"indexed":false,"internalType":"uint16","name":"observationCardinalityNextNew","type":"uint16"}],"name":"IncreaseObservationCardinalityNext","type":"event"},{"anonymous":false,"inputs":[{"indexed":false,"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},{"indexed":false,"internalType":"int24","name":"tick","type":"int24"}],"name":"Initialize","type":"event"},{"anonymous":false,"inputs":[{"indexed":false,"internalType":"address","name":"sender","type":"address"},{"indexed":true,"internalType":"address","name":"owner","type":"address"},{"indexed":true,"internalType":"int24","name":"tickLower","type":"int24"},{"indexed":true,"internalType":"int24","name":"tickUpper","type":"int24"},{"indexed":false,"internalType":"uint128","name":"amount","type":"uint128"},{"indexed":false,"internalType":"uint256","name":"amount0","type":"uint256"},{"indexed":false,"internalType":"uint256","name":"amount1","type":"uint256"}],"name":"Mint","type":"event"},{"anonymous":false,"inputs":[{"indexed":false,"internalType":"uint8","name":"feeProtocol0Old","type":"uint8"},{"indexed":false,"internalType":"uint8","name":"feeProtocol1Old","type":"uint8"},{"indexed":false,"internalType":"uint8","name":"feeProtocol0New","type":"uint8"},{"indexed":false,"internalType":"uint8","name":"feeProtocol1New","type":"uint8"}],"name":"SetFeeProtocol","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"sender","type":"address"},{"indexed":true,"internalType":"address","name":"recipient","type":"address"},{"indexed":false,"internalType":"int256","name":"amount0","type":"int256"},{"indexed":false,"internalType":"int256","name":"amount1","type":"int256"},{"indexed":false,"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},{"indexed":false,"internalType":"uint128","name":"liquidity","type":"uint128"},{"indexed":false,"internalType":"int24","name":"tick","type":"int24"}],"name":"Swap","type":"event"},{"inputs":[{"internalType":"int24","name":"tickLower","type":"int24"},{"internalType":"int24","name":"tickUpper","type":"int24"},{"internalType":"uint128","name":"amount","type":"uint128"}],"name":"burn","outputs":[{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"int24","name":"tickLower","type":"int24"},{"internalType":"int24","name":"tickUpper","type":"int24"},{"internalType":"uint128","name":"amount0Requested","type":"uint128"},{"internalType":"uint128","name":"amount1Requested","type":"uint128"}],"name":"collect","outputs":[{"internalType":"uint128","name":"amount0","type":"uint128"},{"internalType":"uint128","name":"amount1","type":"uint128"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint128","name":"amount0Requested","type":"uint128"},{"internalType":"uint128","name":"amount1Requested","type":"uint128"}],"name":"collectProtocol","outputs":[{"internalType":"uint128","name":"amount0","type":"uint128"},{"internalType":"uint128","name":"amount1","type":"uint128"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"factory","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"fee","outputs":[{"internalType":"uint24","name":"","type":"uint24"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"feeGrowthGlobal0X128","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"feeGrowthGlobal1X128","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"},{"internalType":"bytes","name":"data","type":"bytes"}],"name":"flash","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"uint16","name":"observationCardinalityNext","type":"uint16"}],"name":"increaseObservationCardinalityNext","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"}],"name":"initialize","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"liquidity","outputs":[{"internalType":"uint128","name":"","type":"uint128"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"maxLiquidityPerTick","outputs":[{"internalType":"uint128","name":"","type":"uint128"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"int24","name":"tickLower","type":"int24"},{"internalType":"int24","name":"tickUpper","type":"int24"},{"internalType":"uint128","name":"amount","type":"uint128"},{"internalType":"bytes","name":"data","type":"bytes"}],"name":"mint","outputs":[{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"uint256","name":"","type":"uint256"}],"name":"observations","outputs":[{"internalType":"uint32","name":"blockTimestamp","type":"uint32"},{"internalType":"int56","name":"tickCumulative","type":"int56"},{"internalType":"uint160","name":"secondsPerLiquidityCumulativeX128","type":"uint160"},{"internalType":"bool","name":"initialized","type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"uint32[]","name":"secondsAgos","type":"uint32[]"}],"name":"observe","outputs":[{"internalType":"int56[]","name":"tickCumulatives","type":"int56[]"},{"internalType":"uint160[]","name":"secondsPerLiquidityCumulativeX128s","type":"uint160[]"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"name":"positions","outputs":[{"internalType":"uint128","name":"liquidity","type":"uint128"},{"internalType":"uint256","name":"feeGrowthInside0LastX128","type":"uint256"},{"internalType":"uint256","name":"feeGrowthInside1LastX128","type":"uint256"},{"internalType":"uint128","name":"tokensOwed0","type":"uint128"},{"internalType":"uint128","name":"tokensOwed1","type":"uint128"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"protocolFees","outputs":[{"internalType":"uint128","name":"token0","type":"uint128"},{"internalType":"uint128","name":"token1","type":"uint128"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"uint8","name":"feeProtocol0","type":"uint8"},{"internalType":"uint8","name":"feeProtocol1","type":"uint8"}],"name":"setFeeProtocol","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"slot0","outputs":[{"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},{"internalType":"int24","name":"tick","type":"int24"},{"internalType":"uint16","name":"observationIndex","type":"uint16"},{"internalType":"uint16","name":"observationCardinality","type":"uint16"},{"internalType":"uint16","name":"observationCardinalityNext","type":"uint16"},{"internalType":"uint8","name":"feeProtocol","type":"uint8"},{"internalType":"bool","name":"unlocked","type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"int24","name":"tickLower","type":"int24"},{"internalType":"int24","name":"tickUpper","type":"int24"}],"name":"snapshotCumulativesInside","outputs":[{"internalType":"int56","name":"tickCumulativeInside","type":"int56"},{"internalType":"uint160","name":"secondsPerLiquidityInsideX128","type":"uint160"},{"internalType":"uint32","name":"secondsInside","type":"uint32"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"bool","name":"zeroForOne","type":"bool"},{"internalType":"int256","name":"amountSpecified","type":"int256"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"},{"internalType":"bytes","name":"data","type":"bytes"}],"name":"swap","outputs":[{"internalType":"int256","name":"amount0","type":"int256"},{"internalType":"int256","name":"amount1","type":"int256"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"int16","name":"","type":"int16"}],"name":"tickBitmap","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"tickSpacing","outputs":[{"internalType":"int24","name":"","type":"int24"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"int24","name":"","type":"int24"}],"name":"ticks","outputs":[{"internalType":"uint128","name":"liquidityGross","type":"uint128"},{"internalType":"int128","name":"liquidityNet","type":"int128"},{"internalType":"uint256","name":"feeGrowthOutside0X128","type":"uint256"},{"internalType":"uint256","name":"feeGrowthOutside1X128","type":"uint256"},{"internalType":"int56","name":"tickCumulativeOutside","type":"int56"},{"internalType":"uint160","name":"secondsPerLiquidityOutsideX128","type":"uint160"},{"internalType":"uint32","name":"secondsOutside","type":"uint32"},{"internalType":"bool","name":"initialized","type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]'


def _get_token_name_from_moralis(address: str, moralis_api_key: str) -> (str, str):
    """Get token name and symbol from Moralis API

    Retrieves token name and symbol for given token address from Moralis API.

    Parameters:
        address: str
            Contract address to get ABI for
        moralis_api_key: str
            Moralis API secret key

    Returns:
        tuple: A tuple containing multiple values.
            - element 1 (str): Token name
            - element 2 (str): Token symbol
    """
    url = 'https://deep-index.moralis.io/api/v2.2/erc20/metadata'

    headers = {
        'accept': 'application/json',
        'X-API-Key': moralis_api_key
    }

    params = {
        'chain': 'eth',
        "addresses": [address]
    }

    http_resp = requests.get(url, headers=headers, params=params)

    if http_resp.status_code == 200:
        body = json.loads(http_resp.text)
        return body[0]['name'], body[0]['symbol']
    else:
        return None, None


def _get_token_name(redis_client: StrictRedis, web3: Web3, etherscan_api_key: str, moralis_api_key: str, token_address: any) -> (str, str):
    """Get token name and symbol.

    Retrieves token name and symbol for given token address.

    Checks if token address is present in redis cache, if yes, then retrieves the values from it,
    otherwise the values will be retrieved using web3 library or by calling Moralis API.

    Parameters:
        redis_client: StrictRedis
            Redis client instance
        web3: Web3
            Web3 library instance
        etherscan_api_key: str
            Etherscan.io API secret key
        token_address: Any
            Token address.

    Returns:
        tuple: A tuple containing multiple values.
            - element 1 (str): Token name
            - element 2 (str): Token symbol
    """
    key_token_name_prefix = 'token_name_'
    key_token_name = f'{key_token_name_prefix}{token_address}'
    key_token_symbol_prefix = 'token_symbol_'
    key_token_symbol_name = f'{key_token_symbol_prefix}{token_address}'

    if redis_client.exists(key_token_name) and redis_client.exists(key_token_symbol_name):
        return redis_client.get(key_token_name).decode('utf-8'), redis_client.get(key_token_symbol_name).decode('utf-8')
    else:
        try:
            contract = web3.eth.contract(address=token_address, abi=_get_abi(redis_client, token_address, etherscan_api_key))
            name_result, symbol_result = contract.functions.name().call(), contract.functions.symbol().call()
            redis_client.set(key_token_name, name_result)
            redis_client.set(key_token_symbol_name, symbol_result)
            return name_result, symbol_result
        except (ABIFunctionNotFound, NoABIFunctionsFound):
            token_name, token_symbol = (
                _get_token_name_from_moralis(address=token_address, moralis_api_key=moralis_api_key))
            if token_name is not None and token_symbol is not None:
                redis_client.set(key_token_name, token_name)
                redis_client.set(key_token_symbol_name, token_symbol)
                return token_name, token_symbol
            else:
                return 'PROXY_CONTRACT_UNKNOWN_NAME_API_EXC', 'PROXY_CONTRACT_UNKNOWN_SYMBOL_API_EXC'


def _get_pool_tokens_addresses(redis_client: StrictRedis, pool_address: str, pool_contract: any) -> (str, str):
    """Get pool tokens addresses.

    Retrieves pool tokens addresses from given pool contract

    Checks if pool_address data is present in redis cache, if yes, then retrieves the tokens addresses from it,
    otherwise the values will be retrieved by calling pool contract info RPC provider API.

    Parameters:
        redis_client: StrictRedis
            Redis client instance
        pool_address: str
            Pool address
        pool_contract: any
            Web3 lib pool contract instance (web3.eth.contract class ref.)

    Returns:
        tuple: A tuple containing multiple values.
            - element 1 (str): Pool token0 address
            - element 2 (str): Pool token1 address
    """

    key_pool_address_token0_prefix = 'pool_address_token0_'
    key_pool_token0_address = f'{key_pool_address_token0_prefix}{pool_address}'
    key_pool_address_token1_prefix = 'pool_address_token1_'
    key_pool_token1_address = f'{key_pool_address_token1_prefix}{pool_address}'

    if redis_client.exists(key_pool_token0_address) and redis_client.exists(key_pool_token1_address):
        return redis_client.get(key_pool_token0_address).decode('utf-8'), redis_client.get(key_pool_token1_address).decode('utf-8')
    else:
        token0_address, token1_address = pool_contract.functions.token0().call(), pool_contract.functions.token1().call()
        redis_client.set(key_pool_token0_address, token0_address)
        redis_client.set(key_pool_token1_address, token1_address)
        return token0_address, token1_address


def _parse_row(redis_client, web3, router_codec, etherscan_api_key, moralis_api_key, row: Row):

    # UR transactions tracked commands
    CMD_V2_SWAP_EXACT_IN = 'V2_SWAP_EXACT_IN'
    CMD_V2_SWAP_EXACT_OUT = 'V2_SWAP_EXACT_OUT'
    CMD_V3_SWAP_EXACT_IN = 'V3_SWAP_EXACT_IN'
    CMD_V3_SWAP_EXACT_OUT = 'V3_SWAP_EXACT_OUT'

    row_dict = row.asDict()

    try:
        # Decode transaction input data
        decoded_trx_input = router_codec.decode.function_input(row_dict['input'])

        if row_dict['event_type'] == "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822":  # if UNISWAP_V2_SWAP_EVENT
            tracked_cmds = (CMD_V2_SWAP_EXACT_IN, CMD_V2_SWAP_EXACT_OUT)
        else:                                                                                               # else UNISWAP_V3_SWAP_EVENT
            tracked_cmds = (CMD_V3_SWAP_EXACT_IN, CMD_V3_SWAP_EXACT_OUT)

        filtered_data = [item for item in decoded_trx_input[1]['inputs'] if
                         any(cmd_name in str(item[0]) for cmd_name in tracked_cmds)]

        function, params = filtered_data[0]
        cmd = str(function)

        if CMD_V2_SWAP_EXACT_IN in cmd:
            row_dict['event_type_cmd_identifier'] = CMD_V2_SWAP_EXACT_IN
            row_dict['swap_amount_in'] = str(params['amountIn'])
            row_dict['swap_amount_in_max'] = None
            row_dict['swap_amount_out'] = None
            row_dict['swap_amount_out_min'] = str(params['amountOutMin'])
        elif CMD_V2_SWAP_EXACT_OUT in cmd:
            row_dict['event_type_cmd_identifier'] = CMD_V2_SWAP_EXACT_OUT
            row_dict['swap_amount_in'] = None
            row_dict['swap_amount_in_max'] = str(params['amountInMax'])
            row_dict['swap_amount_out'] = str(params['amountOut'])
            row_dict['swap_amount_out_min'] = None
        elif CMD_V3_SWAP_EXACT_IN in cmd:
            row_dict['event_type_cmd_identifier'] = CMD_V3_SWAP_EXACT_IN
            row_dict['swap_amount_in'] = str(params['amountIn'])
            row_dict['swap_amount_in_max'] = None
            row_dict['swap_amount_out'] = None
            row_dict['swap_amount_out_min'] = str(params['amountOutMin'])
        elif CMD_V3_SWAP_EXACT_OUT in cmd:
            row_dict['event_type_cmd_identifier'] = CMD_V3_SWAP_EXACT_OUT
            row_dict['swap_amount_in'] = None
            row_dict['swap_amount_in_max'] = str(params['amountInMax'])
            row_dict['swap_amount_out'] = str(params['amountOut'])
            row_dict['swap_amount_out_min'] = None

        # Decode pool address swap event data
        pool_address = web3.to_checksum_address(row_dict['pool_address'])
        address_abi = _get_abi(redis_client, pool_address, etherscan_api_key)
        cmd_identifier = row_dict['event_type_cmd_identifier']
        try:
            if len(address_abi) == 0:
                key_prefix = 'contract_'
                key_name = f'{key_prefix}{pool_address}'
                if cmd_identifier in (CMD_V2_SWAP_EXACT_IN, CMD_V2_SWAP_EXACT_OUT):
                    address_abi = _get_uniswap_v2_default_abi()
                    redis_client.set(key_name, address_abi)
                elif cmd_identifier in (CMD_V3_SWAP_EXACT_IN, CMD_V3_SWAP_EXACT_OUT):
                    address_abi = _get_uniswap_v3_default_abi()
                    redis_client.set(key_name, address_abi)
            pool_contract = web3.eth.contract(address=pool_address, abi=address_abi)
            decoded_event = pool_contract.events.Swap().process_log({
                'data': row_dict['data'],
                'topics': [HexBytes(topic) for topic in row_dict['topics'].split(",")],
                'logIndex': row_dict['log_index'],
                'transactionIndex': row_dict['transaction_index'],
                'transactionHash': row_dict['transaction_hash'],
                'address': row_dict['pool_address'],
                'blockHash': row_dict['block_hash'],
                'blockNumber': row_dict['block_number']
            })

            # Get pool token0 and token1 addresses
            token0_address, token1_address = (
                _get_pool_tokens_addresses(redis_client, pool_address=pool_address, pool_contract=pool_contract))

            if cmd_identifier in (CMD_V2_SWAP_EXACT_IN, CMD_V2_SWAP_EXACT_OUT):
                row_dict['v2_amount0In'] = str(decoded_event['args']['amount0In'])
                row_dict['v2_amount1In'] = str(decoded_event['args']['amount1In'])
                row_dict['v2_amount0Out'] = str(decoded_event['args']['amount0Out'])
                row_dict['v2_amount1Out'] = str(decoded_event['args']['amount1Out'])
                row_dict['v3_amount0'] = None
                row_dict['v3_amount1'] = None
                row_dict['v3_sqrtPriceX96'] = None
                row_dict['v3_liquidity'] = None
                row_dict['v3_tick'] = None

                # v2_amount0In > 0 means that pool token0 is an INPUT in Uniswap V2 swap event, v2_amount1Out > 0 means that pool token1 is OUTPUT
                if (int(row_dict['v2_amount0In']) > 0) and (int(row_dict['v2_amount1Out']) > 0):
                    row_dict['token_in_address'] = str(token0_address)
                    row_dict['token_in_name'], row_dict['token_in_symbol'] = (
                        _get_token_name(redis_client, web3, etherscan_api_key, moralis_api_key, token0_address))
                    row_dict['token_out_address'] = str(token1_address)
                    row_dict['token_out_name'], row_dict['token_out_symbol'] = (
                        _get_token_name(redis_client, web3, etherscan_api_key, moralis_api_key, token1_address))
                # Otherwise pool token0 is OUTPUT, pool token1 is INPUT
                else:
                    row_dict['token_in_address'] = str(token1_address)
                    row_dict['token_in_name'], row_dict['token_in_symbol'] = (
                        _get_token_name(redis_client, web3, etherscan_api_key, moralis_api_key, token1_address))
                    row_dict['token_out_address'] = str(token0_address)
                    row_dict['token_out_name'], row_dict['token_out_symbol'] = (
                        _get_token_name(redis_client, web3, etherscan_api_key, moralis_api_key, token0_address))

            elif cmd_identifier in (CMD_V3_SWAP_EXACT_IN, CMD_V3_SWAP_EXACT_OUT):
                row_dict['v2_amount0In'] = None
                row_dict['v2_amount1In'] = None
                row_dict['v2_amount0Out'] = None
                row_dict['v2_amount1Out'] = None
                row_dict['v3_amount0'] = str(decoded_event['args']['amount0'])
                row_dict['v3_amount1'] = str(decoded_event['args']['amount1'])
                row_dict['v3_sqrtPriceX96'] = str(decoded_event['args']['sqrtPriceX96'])
                row_dict['v3_liquidity'] = str(decoded_event['args']['liquidity'])
                row_dict['v3_tick'] = str(decoded_event['args']['tick'])

                # v3_amount0 > 0 means that pool token0 is an INPUT in Uniswap V3 swap event, v3_amount1 < 0 means that pool token1 is OUTPUT
                if (int(row_dict['v3_amount0']) > 0) and (int(row_dict['v3_amount1']) < 0):
                    row_dict['token_in_address'] = str(token0_address)
                    row_dict['token_in_name'], row_dict['token_in_symbol'] = (
                        _get_token_name(redis_client, web3, etherscan_api_key, moralis_api_key, token0_address))
                    row_dict['token_out_address'] = str(token1_address)
                    row_dict['token_out_name'], row_dict['token_out_symbol'] = (
                        _get_token_name(redis_client, web3, etherscan_api_key, moralis_api_key, token1_address))
                # Otherwise pool token0 is OUTPUT, pool token1 is INPUT
                else:
                    row_dict['token_in_address'] = str(token1_address)
                    row_dict['token_in_name'], row_dict['token_in_symbol'] = (
                        _get_token_name(redis_client, web3, etherscan_api_key, moralis_api_key, token1_address))
                    row_dict['token_out_address'] = str(token0_address)
                    row_dict['token_out_name'], row_dict['token_out_symbol'] = (
                        _get_token_name(redis_client, web3, etherscan_api_key, moralis_api_key, token0_address))
        except NoABIEventsFound:
            callback_msg = 'POOL_ADDRESS_SRC_CODE_NOT_VERIFIED_EXC'
            row_dict['event_type_cmd_identifier'] = callback_msg
            row_dict['token_in_address'] = None
            row_dict['token_in_name'], row_dict['token_in_symbol'] = None, None
            row_dict['token_out_address'] = None
            row_dict['token_out_name'], row_dict['token_out_symbol'] = None, None
            row_dict['v2_amount0In'] = None
            row_dict['v2_amount1In'] = None
            row_dict['v2_amount0Out'] = None
            row_dict['v2_amount1Out'] = None
            row_dict['v3_amount0'] = None
            row_dict['v3_amount1'] = None
            row_dict['v3_sqrtPriceX96'] = None
            row_dict['v3_liquidity'] = None
            row_dict['v3_tick'] = None

    except (InsufficientDataBytes, IndexError, Exception) as e:
        if isinstance(e, InsufficientDataBytes):
            callback_msg = 'INSUFFICIENT_DATA_BYTES_EXC'
        elif isinstance(e, IndexError):
            callback_msg = 'INDEX_PARSING_EXC'
        else:
            callback_msg = 'UNKNOWN_EXC'

        row_dict['event_type_cmd_identifier'] = callback_msg
        row_dict['token_in_address'] = None
        row_dict['token_in_name'], row_dict['token_in_symbol'] = None, None
        row_dict['token_out_address'] = None
        row_dict['token_out_name'], row_dict['token_out_symbol'] = None, None
        row_dict['swap_amount_in'] = None
        row_dict['swap_amount_in_max'] = None
        row_dict['swap_amount_out'] = None
        row_dict['swap_amount_out_min'] = None
        row_dict['v2_amount0In'] = None
        row_dict['v2_amount1In'] = None
        row_dict['v2_amount0Out'] = None
        row_dict['v2_amount1Out'] = None
        row_dict['v3_amount0'] = None
        row_dict['v3_amount1'] = None
        row_dict['v3_sqrtPriceX96'] = None
        row_dict['v3_liquidity'] = None
        row_dict['v3_tick'] = None

    del row_dict['to_address']  # always UR contract address (0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad)
    del row_dict['input']
    del row_dict['data']
    del row_dict['topics']

    return Row(**row_dict)


def _parse_partition(iterator):
    redis_client = StrictRedis(host=os.getenv("REDIS_HOST"),
                               port=int(os.getenv("REDIS_PORT")),
                               db=int(os.getenv("REDIS_DB")))
    rpc_provider = Web3.HTTPProvider(os.getenv('RPC_ANKR_HTTPS_ENDPOINT'))
    web3 = Web3(provider=rpc_provider)
    router_codec = RouterCodec(w3=web3)

    for row in iterator:
        yield _parse_row(redis_client, web3, router_codec, os.getenv("ETHERSCAN_API_KEY"), os.getenv("MORALIS_API_KEY"), row)

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

    schema = StructType([StructField('transaction_hash', StringType(), False),
                         StructField('sender_address', StringType(), False),
                         StructField('value', StringType(), True),
                         StructField('gas', StringType(), True),
                         StructField('gas_price', StringType(), True),
                         StructField('block_timestamp', StringType(), False),
                         StructField('max_fee_per_gas', StringType(), True),
                         StructField('max_priority_fee_per_gas', StringType(), True),
                         StructField('transaction_type', StringType(), True),
                         StructField('log_index', StringType(), False),
                         StructField('transaction_index', StringType(), False),
                         StructField('block_hash', StringType(), False),
                         StructField('block_number', StringType(), False),
                         StructField('pool_address', StringType(), False),
                         StructField('event_type', StringType(), False),
                         StructField('transaction_swap_number', StringType(), False),
                         StructField('event_type_cmd_identifier', StringType(), False),
                         StructField('swap_amount_in', StringType(), True),
                         StructField('swap_amount_in_max', StringType(), True),
                         StructField('swap_amount_out', StringType(), True),
                         StructField('swap_amount_out_min', StringType(), True),
                         StructField('v2_amount0In', StringType(), True),
                         StructField('v2_amount1In', StringType(), True),
                         StructField('v2_amount0Out', StringType(), True),
                         StructField('v2_amount1Out', StringType(), True),
                         StructField('v3_amount0', StringType(), True),
                         StructField('v3_amount1', StringType(), True),
                         StructField('v3_sqrtPriceX96', StringType(), True),
                         StructField('v3_liquidity', StringType(), True),
                         StructField('v3_tick', StringType(), True),
                         StructField('token_in_address', StringType(), True),
                         StructField('token_in_name', StringType(), True),
                         StructField('token_in_symbol', StringType(), True),
                         StructField('token_out_address', StringType(), True),
                         StructField('token_out_name', StringType(), True),
                         StructField('token_out_symbol', StringType(), True), ])

    eth_blockchain_transactions_df = (transactions_df
                                      .select(*[col(column) for column in eth_blockchain_transactions_df_columns])
                                      .filter(col('to_address').isin(UNIVERSAL_ROUTER_CONTRACT_ADDRESS))
                                      .withColumnRenamed('hash', 'transaction_hash')
                                      .withColumnRenamed('from_address', 'sender_address'))

    eth_blockchain_logs_df = (logs_df
                              .select(*[col(column) for column in eth_blockchain_logs_df_columns])
                              .withColumn('event_type', split(logs_df['topics'], ",").getItem(0))
                              .filter(col('event_type').isin(TRACKED_EVENTS))
                              .withColumn('swap_number_per_transaction', row_number()  # mark swaps per single transaction
                                          .over(Window.partitionBy("transaction_hash").orderBy("log_index")))
                              .withColumnRenamed('address', 'pool_address'))

    result = (eth_blockchain_transactions_df
              .join(eth_blockchain_logs_df, "transaction_hash")
              .cache()
              .rdd.mapPartitions(_parse_partition)
              .toDF(schema=schema))

    return result


def write_to_s3(df: DataFrame, s3_result_uri: str) -> None:
    (df
     .repartition(1)
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
