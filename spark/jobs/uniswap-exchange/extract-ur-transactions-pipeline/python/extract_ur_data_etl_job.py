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
    except (json.decoder.JSONDecodeError, requests.exceptions.JSONDecodeError, requests.exceptions.SSLError, requests.exceptions.ConnectionError):
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


def _get_token_name(redis_client: StrictRedis, web3: Web3, etherscan_api_key: str, token_address: any) -> (str, str):
    """Get token name and symbol.

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
        contract = web3.eth.contract(address=token_address, abi=_get_abi(redis_client, token_address, etherscan_api_key))
        try:
            name_result, symbol_result = contract.functions.name().call(), contract.functions.symbol().call()
            redis_client.set(key_token_name, name_result)
            redis_client.set(key_token_symbol_name, symbol_result)
            return name_result, symbol_result
        except (ABIFunctionNotFound, NoABIFunctionsFound):
            proxy_contracts = {     # proxy contracts map (detected tokens whose metadata is hidden behind a proxy)
                '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48': {'name': 'USD Coin', 'symbol': 'USDC'},   # ex. USDC -> https://etherscan.io/address/0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48
                '0xf65B5C5104c4faFD4b709d9D60a185eAE063276c': {'name': 'Truebit', 'symbol': 'TRU'},
                '0xD9A442856C234a39a81a089C06451EBAa4306a72': {'name': 'Puffer Finance', 'symbol': 'pufETH'},
                '0x069d89974f4edaBde69450f9cF5CF7D8Cbd2568D': {'name': 'BVM', 'symbol': 'BVM'},
                '0x614577036F0a024DBC1C88BA616b394DD65d105a': {'name': 'Genius Token', 'symbol': 'GNUS'},
                '0xE2cfD7a01ec63875cd9Da6C7c1B7025166c2fA2F': {'name': 'Hyper', 'symbol': 'HYPER'},
                '0x046EeE2cc3188071C02BfC1745A6b17c656e3f3d': {'name': 'Rollbit Coin', 'symbol': 'RLB'},
                '0x8e729198d1C59B82bd6bBa579310C40d740A11C2': {'name': 'Alvara', 'symbol': 'ALVA'},
                '0x1151CB3d861920e07a38e03eEAd12C32178567F6': {'name': 'Bonk', 'symbol': 'Bonk'},
                '0xD46bA6D942050d489DBd938a2C909A5d5039A161': {'name': 'Ampleforth', 'symbol': 'AMPL'},
                '0x590F820444fA3638e022776752c5eEF34E2F89A6': {'name': 'Alephium (AlphBridge)', 'symbol': 'ALPH'},
                '0xBAac2B4491727D78D2b78815144570b9f2Fe8899': {'name': 'The Doge NFT', 'symbol': 'DOG'},
                '0x0fff882394fdE8c5D547764027E16bA1b9E84155': {'name': 'DIAMOND', 'symbol': 'DIAMOND'},
                '0xd1d2Eb1B1e90B638588728b4130137D262C87cae': {'name': 'Gala Games', 'symbol': 'GALA'},
                '0x89E8E084cC60e6988527f0904b4bE71656e8bFA9': {'name': 'Smog', 'symbol': 'SMOG'},
                '0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84': {'name': 'stETH', 'symbol': 'stETH'},
                '0x5dE869E3E62B0FB2c15573246BA3Bb3Fd97a2275': {'name': 'Sheboshis', 'symbol': 'SHEB'},
                '0xB50721BCf8d664c30412Cfbc6cf7a15145234ad1': {'name': 'Arbitrum', 'symbol': 'ARB'},
                '0xf0f9D895aCa5c8678f706FB8216fa22957685A13': {'name': 'Cult DAO', 'symbol': 'CULT'},
                '0x1614F18Fc94f47967A3Fbe5FfcD46d4e7Da3D787': {'name': 'PAID Network', 'symbol': 'PAID'},
                '0x9cAAe40DCF950aFEA443119e51E821D6FE2437ca': {'name': 'Blocjerk', 'symbol': 'BJ'},
                '0x411099C0b413f4fedDb10Edf6a8be63BD321311C': {'name': 'Hello', 'symbol': 'HELLO'},
                '0x6De037ef9aD2725EB40118Bb1702EBb27e4Aeb24': {'name': 'Render Token', 'symbol': 'RNDR'},
                '0x25931894a86D47441213199621F1F2994e1c39Aa': {'name': 'ChainGPT', 'symbol': 'CGPT'},
                '0x6E2a43be0B1d33b726f0CA3b8de60b3482b8b050': {'name': 'Arkham', 'symbol': 'ARKM'},
                '0x672f4fa517894496B8A958b4b3fcA068cE513A39': {'name': 'DexCheck', 'symbol': 'DCK'},
                '0xbf5495Efe5DB9ce00f80364C8B423567e58d2110': {'name': 'Renzo Restaked ETH', 'symbol': 'ezETH'},
                '0x31aDdA225642a8f4D7e90d4152BE6661ab22a5a2': {'name': 'HYPR', 'symbol': 'HYPR'},
                '0xf951E335afb289353dc249e82926178EaC7DEd78': {'name': 'swETH', 'symbol': 'swETH'},
                '0xA1290d69c65A6Fe4DF752f95823fae25cB99e5A7': {'name': 'rsETH', 'symbol': 'rsETH'},
                '0xFf44b937788215ecA197BAaf9AF69dbdC214aa04': {'name': 'ROCKI', 'symbol': 'ROCKI'},
                '0xBEF26Bd568e421D6708CCA55Ad6e35f8bfA0C406': {'name': 'bitsCrunch Token', 'symbol': 'BCUT'},
                '0x12eF10A4fc6e1Ea44B4ca9508760fF51c647BB71': {'name': 'Restake Finance', 'symbol': 'RSTK'},
                '0x3419875B4D3Bca7F3FddA2dB7a476A79fD31B4fE': {'name': 'DizzyHavoc', 'symbol': 'DZHV'},
                '0x644192291cc835A93d6330b24EA5f5FEdD0eEF9e': {'name': 'AllianceBlock Nexera Token', 'symbol': 'NXRA'},
                '0x99B600D0a4abdbc4a6796225a160bCf3D5Ce2a89': {'name': 'Solareum', 'symbol': 'SRM'},
                '0x2a3bFF78B79A009976EeA096a51A948a3dC00e34': {'name': 'Wilder', 'symbol': 'WILD'},
                '0x8E3A59427B1D87Db234Dd4ff63B25E4BF94672f4': {'name': 'Kelp Earned Point', 'symbol': 'KEP'},
                '0xD8c0b13B551718b808fc97eAd59499d5Ef862775': {'name': 'Gala Music', 'symbol': '$MUSIC'},
                '0xBe9895146f7AF43049ca1c1AE358B0541Ea49704': {'name': 'Coinbase Wrapped Staked ETH', 'symbol': 'cbETH'},
                '0x3c3a81e81dc49A522A592e7622A7E711c06bf354': {'name': 'Mantle', 'symbol': 'MNT'},
                '0x9Ce84F6A69986a83d92C324df10bC8E64771030f': {'name': 'Chintai Exchange Token', 'symbol': 'CHEX'},
                '0xB7Cfe05915Ef0c040c6ddE2007c9ddaB26259E04': {'name': 'Molly', 'symbol': 'MOLLY'},
                '0x85f138bfEE4ef8e540890CFb48F620571d67Eda3': {'name': 'Wrapped AVAX', 'symbol': 'WAVAX'},
                '0x53A0D4180182c943F416a74eA5E29b78a979055a': {'name': 'Pepechain', 'symbol': 'PC'},
                '0x67268687E26d0F34cAc5DE30E6c2F63fACB592Bd': {'name': 'All-In-One', 'symbol': 'AI1'},
                '0xbededDf2eF49E87037c4fb2cA34d1FF3D3992A11': {'name': 'FEG Token', 'symbol': 'FEG'},
                '0x3496B523e5C00a4b4150D6721320CdDb234c3079': {'name': 'NUM Token', 'symbol': 'NUM'},
                '0x45804880De22913dAFE09f4980848ECE6EcbAf78': {'name': 'Paxos Gold', 'symbol': 'PAXG'},
                '0xC52C326331E9Ce41F04484d3B5E5648158028804': {'name': 'ZEN Exchange Token', 'symbol': 'ZCX'},
                '0xE0C8b298db4cfFE05d1bEA0bb1BA414522B33C1B': {'name': 'NCDToken', 'symbol': 'NCDT'},
                '0xd5F7838F5C461fefF7FE49ea5ebaF7728bB0ADfa': {'name': 'mETH', 'symbol': 'mETH'},
                '0x39D5313C3750140E5042887413bA8AA6145a9bd2': {'name': 'Empyreal', 'symbol': 'EMP'},
                '0x9609831BB1d77DE5eA10e18c9c718cB6c37D4284': {'name': 'MEME Kong', 'symbol': 'MKONG'},
                '0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9': {'name': 'Aave Token', 'symbol': 'AAVE'},
                '0xFAe103DC9cf190eD75350761e95403b7b8aFa6c0': {'name': 'rswETH', 'symbol': 'rswETH'},
                '0x64323d606CfCB1b50998636A182334Ad97637987': {'name': 'xperp', 'symbol': 'xperp'},
                '0xae78736Cd615f374D3085123A210448E74Fc6393': {'name': 'Rocket Pool ETH', 'symbol': 'rETH'},
                '0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee': {'name': 'Wrapped eETH', 'symbol': 'weETH'},
                '0x3294395e62F4eB6aF3f1Fcf89f5602D90Fb3Ef69': {'name': 'Celo native asset (Wormhole)', 'symbol': 'CELO'},
                '0x395E925834996e558bdeC77CD648435d620AfB5b': {'name': 'TFT on Ethereum', 'symbol': 'TFT'},
                '0xE55d97A97ae6A17706ee281486E98A84095d8AAf': {'name': 'AIPAD.tech', 'symbol': 'AIPAD'},
                '0xFe6C35e99398e488438501da011Ea99695F6aC29': {'name': 'TensorMoon', 'symbol': 'TSM'},
                '0x4819aC09e4619748b1cDF657283A948731fa6Ab6': {'name': 'ARCrypto', 'symbol': 'ENZF'},
                '0x3aFfCCa64c2A6f4e3B6Bd9c64CD2C969EFd1ECBe': {'name': 'DSLA', 'symbol': 'DSLA'},
                '0x41545f8b9472D758bB669ed8EaEEEcD7a9C4Ec29': {'name': 'Forta', 'symbol': 'FORT'},
                '0x48C3399719B582dD63eB5AADf12A40B4C3f52FA2': {'name': 'StakeWise', 'symbol': 'SWISE'},
                '0xB7B1570e26315BAAD369b8EA0a943b7F140DB9eB': {'name': 'DEEPSPACE', 'symbol': 'DPS'},
                '0x52498F8d9791736f1D6398fE95ba3BD868114d10': {'name': 'NetVRk', 'symbol': 'NETVR'},
                '0x8FAc8031e079F409135766C7d5De29cf22EF897C': {'name': 'HUMANS', 'symbol': 'HEART'},
                '0x0e6FA9c050c8a707e7f56A2b3695665E4F9Eac9b': {'name': 'ROBO INU', 'symbol': 'RBIF'},
                '0xB4b9DC1C77bdbb135eA907fd5a08094d98883A35': {'name': 'SWEAT', 'symbol': 'SWEAT'},
                '0x0e4e7F2AecF408AFF4f82f067677050239bdC58A': {'name': 'Fung Token', 'symbol': 'FUNG'},
                '0x6e1649cEf61Fa17d647B9324D905C33fea71D020': {'name': 'Good DooKwon', 'symbol': 'GOODDK'},
                '0x1416946162B1C2c871A73B07E932D2fB6C932069': {'name': 'Energi', 'symbol': 'NRG'},
                '0x07150e919B4De5fD6a63DE1F9384828396f25fDC': {'name': 'Base Protocol', 'symbol': 'BASE'},
                '0x4da0C48376C277cdBd7Fc6FdC6936DEE3e4AdF75': {'name': 'Epik Prime', 'symbol': 'EPIK'},
                '0x954b890704693af242613edEf1B603825afcD708': {'name': 'Cardstack', 'symbol': 'CARD'},
                '0x0d02755a5700414B26FF040e1dE35D337DF56218': {'name': 'Bend Token', 'symbol': 'BEND'},
                '0x90DE74265a416e1393A450752175AED98fe11517': {'name': 'Unlock Discount Token', 'symbol': 'UDT'},
                '0x4740735AA98Dc8aa232BD049f8F0210458E7fCa3': {'name': 'Ridotto', 'symbol': 'RDT'},
                '0xB0c7a3Ba49C7a6EaBa6cD4a96C55a1391070Ac9A': {'name': 'MAGIC', 'symbol': 'MAGIC'},
                '0x0345173a92742e9dAF55d44Ac65e0D987b22379e': {'name': 'ETH Monsta', 'symbol': 'METH'},
                '0x60F5672A271C7E39E787427A18353ba59A4A3578': {'name': 'PIKA', 'symbol': 'PIKA'},
                '0x3A856d4effa670C54585a5D523e96513e148e95d': {'name': 'Trias Token', 'symbol': 'TRIAS'},
                '0x82F13aB56Cc0D1b727E8253a943f0DE75B048b0B': {'name': 'PlayFi', 'symbol': 'PLAYFI'},
                '0xA35b1B31Ce002FBF2058D22F30f95D405200A15b': {'name': 'ETHx', 'symbol': 'ETHx'},
                '0xeb986DA994E4a118d5956b02d8b7c3C7CE373674': {'name': 'Gather', 'symbol': 'GTH'},
                '0x64a86BbE4ec8452aD0743aE68875048C7Af47A2c': {'name': 'Bad DooKwon', 'symbol': 'BADDK'},
                '0x18aAA7115705e8be94bfFEBDE57Af9BFc265B998': {'name': 'Audius', 'symbol': 'AUDIO'},
                '0xE7f58A92476056627f9FdB92286778aBd83b285F': {'name': 'Decentraweb', 'symbol': 'DWEB'},
                '0xdDFbE9173c90dEb428fdD494cB16125653172919': {'name': 'Sowaka', 'symbol': 'SWK'},
                '0x137cD8Baa677F2F12e43446709615f5226C69088': {'name': 'Mindx', 'symbol': 'MAI'},
                '0xaBB56755B11c0752E168178E15A3ccAD8d2f7d8E': {'name': 'Squid TAO', 'symbol': 'STAO'},
                '0x4295c8556AFEE00264C0789dde2ddd2dba71AcFe': {'name': 'Bidao Smart Chain', 'symbol': 'BISC'},
                '0xCC4304A31d09258b0029eA7FE63d032f52e44EFe': {'name': 'TrustSwap Token', 'symbol': 'SWAP'},
                '0x68b429161Ec09a6c1D65ba70727AB1faa5Bc4026': {'name': 'Ordinal Doge', 'symbol': 'oDoge'},
                '0xe96938e0D086A241D03688ddA697Bf57859Ee261': {'name': 'Buccaneer V3', 'symbol': 'BUCC'},
                '0xEC213F83defB583af3A000B1c0ada660b1902A0F': {'name': 'Presearch', 'symbol': 'PRE'},
                '0xADe6FDAba1643E4D1eeF68Da7170F234470938c6': {'name': 'Harambe', 'symbol': 'HARAMBE'},
                '0xaF1eEb83c364aD9ffeb5f97F223C1705D4810033': {'name': 'MoneyArk Token', 'symbol': 'Mark'},
                '0xbC0899E527007f1B8Ced694508FCb7a2b9a46F53': {'name': 'BSKT', 'symbol': 'BSKT'},
                '0xB55271206D3ff1FcA6eE17e9F4175e4AF379D0c3': {'name': 'Peng', 'symbol': '$PENG'},
                '0x8a74BC8c372bC7f0E9cA3f6Ac0df51BE15aEC47A': {'name': 'PULSEPAD.io', 'symbol': 'PLSPAD'},
                '0xA49d7499271aE71cd8aB9Ac515e6694C755d400c': {'name': 'Mute.io', 'symbol': 'MUTE'},
                '0x88dF592F8eb5D7Bd38bFeF7dEb0fBc02cf3778a0': {'name': 'Tellor Tributes', 'symbol': 'TRB'},
                '0x7a2Bc711E19ba6aff6cE8246C546E8c4B4944DFD': {'name': 'WAX Economic Token', 'symbol': 'WAXE'},
                '0x4AC429A7cdf2b533E2c0cFF1b017F2c344E864e2': {'name': 'PulseX from PulseChain', 'symbol': 'PLSX'},
                '0x785c34312dfA6B74F6f1829f79ADe39042222168': {'name': 'BUMP', 'symbol': 'BUMP'},
                '0xFe0979126C2fd8a1889ADCfdB5dCC3809Ea2F999': {'name': 'T-0', 'symbol': 'T-0'},
                '0x329c6E459FFa7475718838145e5e85802Db2a303': {'name': 'MaidSafeCoin', 'symbol': 'eMAID'},
                '0x6ef3D766Dfe02Dc4bF04aAe9122EB9A0Ded25615': {'name': 'Prime Staked ETH', 'symbol': 'primeETH'},
                '0xd4A49B3394FbaedaF43F527E30eB01498a4eD346': {'name': 'Decentralized EVM Verification Layer', 'symbol': 'DeVL'},
                '0xEE1CeA7665bA7aa97e982EdeaeCb26B59a04d035': {'name': 'ORAO Network', 'symbol': 'ORAO'},
                '0xb6ADB74efb5801160Ff749b1985Fd3bD5000e938': {'name': 'GAMEZONE.io', 'symbol': 'GZONE'},
                '0xfc74462a2f6968779c64a27a5E974Ccd7056E8e8': {'name': 'InsureX', 'symbol': 'INSX'},
                '0x67d85A291fcDC862A78812a3C26d55e28FFB2701': {'name': 'Asymetrix Governance Token', 'symbol': 'ASX'},
                '0xEA81DaB2e0EcBc6B5c4172DE4c22B6Ef6E55Bd8f': {'name': 'Plebbit', 'symbol': 'PLEB'},
                '0xF17e65822b568B3903685a7c9F496CF7656Cc6C2': {'name': 'Biconomy Token', 'symbol': 'BICO'},
                '0x8dBF9A4c99580fC7Fd4024ee08f3994420035727': {'name': 'ECO', 'symbol': 'ECO'},
                '0x580c8520dEDA0a441522AEAe0f9F7A5f29629aFa': {'name': 'Dawn', 'symbol': 'DAWN'},
                '0x8dB1D28Ee0d822367aF8d220C0dc7cB6fe9DC442': {'name': 'ETHPAD.network', 'symbol': 'ETHPAD'},
                '0xe20B9e246db5a0d21BF9209E4858Bc9A3ff7A034': {'name': 'Wrapped Banano', 'symbol': 'wBAN'},
                '0xAf2CA40d3fc4459436D11B94d21FA4b8A89fB51d': {'name': 'gCOTI Token', 'symbol': 'gCOTI'},
                '0x97Ac4a2439A47c07ad535bb1188c989dae755341': {'name': 'Wrapped Pulse from PulseChain', 'symbol': 'WPLS'},
                '0xeD35af169aF46a02eE13b9d79Eb57d6D68C1749e': {'name': 'OMI Token', 'symbol': 'OMI'},
                '0x79F05c263055BA20EE0e814ACD117C20CAA10e0c': {'name': 'Ice', 'symbol': 'ICE'},
                '0x5fab9761d60419C9eeEbe3915A8FA1ed7e8d2E1B': {'name': 'Dimo', 'symbol': 'DIMO'},
                '0x99BB69Ee1BbFC7706C3ebb79b21C5B698fe58EC0': {'name': 'DeHub', 'symbol': 'DHB'},
                '0x62858686119135cc00C4A3102b436a0eB314D402': {'name': 'METAVPAD.com', 'symbol': 'METAV'},
                '0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409': {'name': 'First Digital USD', 'symbol': 'FDUSD'},
                '0x263B6B028F3e4eD8C4329eB2b5f409Ee38D97296': {'name': 'worldmobiletoken', 'symbol': 'WMT'},
                '0x5ca5aB9A37b6700c45237aDb3652928f3E0b75fB': {'name': 'Doggensnout Skeptic', 'symbol': 'Doggensnout Skeptic'},
                '0x01824357D7D7EAF4677Bc17786aBd26CBdEc9Ad7': {'name': 'Forward', 'symbol': '$FORWARD'},
                '0x13e1DE067ff2F1E1335a48319AfCcDf8a995043C': {'name': 'Wrapped Nuni', 'symbol': 'wNuni'},
                '0xB60acD2057067DC9ed8c083f5aa227a244044fD6': {'name': 'Tensorplex Staked TAO', 'symbol': 'stTAO'},
                '0x4385328cc4D643Ca98DfEA734360C0F596C83449': {'name': 'tomi Token', 'symbol': 'TOMI'},
                '0x6c3ea9036406852006290770BEdFcAbA0e23A0e8': {'name': 'PayPal USD', 'symbol': 'PYUSD'},
                '0xa62cc35625B0C8dc1fAEA39d33625Bb4C15bD71C': {'name': 'StormX', 'symbol': 'STMX'},
                '0xdDc6625FEcA10438857DD8660C021Cd1088806FB': {'name': 'Radcoin V2', 'symbol': 'RAD'},
                '0x0C37Bcf456bC661C14D596683325623076D7e283': {'name': 'Aeron', 'symbol': 'ARNX'},
                '0x2f7098696Aac7C114A013229C3C752e41B07e80f': {'name': 'Decentralized Stable Asset', 'symbol': 'DSA'},
                '0xAAfec8e08d524D534327fa13fB306F440B5F88EB': {'name': 'wCTC', 'symbol': 'wCTC'},
                '0xC90Dd9d2c7778C33A3b189D030af8C4b6A474Ac3': {'name': 'BOOK OF MEME', 'symbol': 'BOME'},
                '0xfcfC434ee5BfF924222e084a8876Eee74Ea7cfbA': {'name': 'Rebasing Liquidity Token - DELTA.financial', 'symbol': 'DELTA rLP'},
                '0xAb6c558689c409EB284dE390DA19DACAB2d7C148': {'name': 'Barbecue', 'symbol': 'Barbecue'},
                '0x57B946008913B82E4dF85f501cbAeD910e58D26C': {'name': 'Marlin POND', 'symbol': 'POND'},
                '0x9c354503C38481a7A7a51629142963F98eCC12D0': {'name': 'Origin Dollar Governance', 'symbol': 'OGV'},
                '0x55D08aaE2A4f75FbFb70A370E3B0CDEC30229628': {'name': 'DarwinAI', 'symbol': 'DarwinAI'},
                '0x8193711b2763Bc7DfD67Da0d6C8c26642eafDAF3': {'name': 'INSTAR', 'symbol': 'INSTAR'},
                '0x58fB30A61C218A3607e9273D52995a49fF2697Ee': {'name': 'PINT', 'symbol': 'PINT'},
                '0x1487Bd704Fa05A222B0aDB50dc420f001f003045': {'name': 'Cross-Chain DAO', 'symbol': 'CCDAO'},
                '0xF17A3fE536F8F7847F1385ec1bC967b2Ca9caE8D': {'name': 'Alongside Crypto Market Index', 'symbol': 'AMKT'},
                '0xa5E869A7E871ec0039E4478371E7f745C279839C': {'name': 'Kiwi', 'symbol': 'KIWI'},
                '0x96F6eF951840721AdBF46Ac996b59E0235CB985C': {'name': 'Ondo U.S. Dollar Yield', 'symbol': 'USDY'},
                '0x454ea94dA3333B4333a8fF43Fca4bf128522a53F': {'name': 'BEAR from PulseChain', 'symbol': 'TEDDY BEAR ㉾'},
                '0x50094DF702Ff3cb6847BE899b4A9f0b23Fd841Ed': {'name': 'nai', 'symbol': 'NAI'},
                '0xA1d87e02b859e908b01fE2247456Ac2e9D538c1e': {'name': 'Elsa', 'symbol': 'ELSA'},
                '0xE95A203B1a91a908F9B9CE46459d101078c2c3cb': {'name': 'Ankr Staked ETH', 'symbol': 'ankrETH'},
                '0x6a26ee7755d939b96cdCd8A0370630D48902FD7C': {'name': 'MAGIKAL.ai', 'symbol': 'MGKL'},
                '0x36F02D589F5E86cA578777098a225cb1F1e33986': {'name': 'Danklin', 'symbol': 'DANKLIN'},
                '0xD291E7a03283640FDc51b121aC401383A46cC623': {'name': 'Rari Governance Token', 'symbol': 'RGT'},
                '0x450E7F6e3a2f247A51b98C39297a9a5BfbDb3170': {'name': 'ElonGoat', 'symbol': 'EGT'},
                '0xb8e3bB633F7276cc17735D86154E0ad5ec9928C0': {'name': 'VELASPAD.io', 'symbol': 'VLXPAD'},
                '0xDd1Ad9A21Ce722C151A836373baBe42c868cE9a4': {'name': 'Universal Basic Income', 'symbol': 'UBI'},
                '0x418D75f65a02b3D53B2418FB8E1fe493759c7605': {'name': 'Wrapped BNB (Wormhole)', 'symbol': 'WBNB'},
                '0x11E003e9eCC5a2320E8b11098ACd550b928b6df2': {'name': 'Xi Transfer Coin', 'symbol': 'XXi'},
                '0x60EF10EDfF6D600cD91caeCA04caED2a2e605Fe5': {'name': 'Mochi Inu', 'symbol': 'MOCHI'},
                '0x3B6564b5DA73A41d3A66e6558a98FD0e9E1e77ad': {'name': 'Unitus', 'symbol': 'UTS'},
                '0xE28027c99C7746fFb56B0113e5d9708aC86fAE8f': {'name': 'KING', 'symbol': 'KING'},
                '0x4Fabb145d64652a948d72533023f6E7A623C7C53': {'name': 'BUSD', 'symbol': 'BUSD'},
                '0xeEEE2a622330E6d2036691e983DEe87330588603': {'name': 'Askobar Network', 'symbol': 'ASKO'},
                '0xC4C2614E694cF534D407Ee49F8E44D125E4681c4': {'name': 'Chain Games', 'symbol': 'CHAIN'},
                '0xbddc20ed7978B7d59eF190962F441cD18C14e19f': {'name': 'Crypto Asset Governance Alliance', 'symbol': 'CAGA'},
                '0x9C38688E5ACB9eD6049c8502650db5Ac8Ef96465': {'name': 'Lif', 'symbol': 'LIF'},
                '0x2A79324c19Ef2B89Ea98b23BC669B7E7c9f8A517': {'name': 'WAXP Token', 'symbol': 'WAXP'},
                '0x39D8532f166890e2A057B7Cc6b95E4A71Ac3BbF3': {'name': 'iVoice AI', 'symbol': 'IVOICE'},
                '0x0eC78ED49C2D27b315D462d43B5BAB94d2C79bf8': {'name': 'MEOW', 'symbol': 'MEOW'},
                '0xf5AEd4F6a1Ad00f39Dd21febb6f400ea020030c2': {'name': 'Hodless BOT', 'symbol': 'HBOT'},
                '0x3B69e6670a81C6c5027c5EbBEaD55B2D08AD5b84': {'name': 'Cenτsor Protocol', 'symbol': 'CENT'},
                '0x7F3EDcdD180Dbe4819Bd98FeE8929b5cEdB3AdEB': {'name': 'xToken', 'symbol': 'XTK'},
                '0x7FA7dF4996AC59F398476892cfB195eD38543520': {'name': 'WAGYUSWAP.app', 'symbol': 'WAG'},
                '0x95987b0cdC7F65d989A30B3B7132a38388c548Eb': {'name': 'PURSE TOKEN', 'symbol': 'PURSE'},
                '0x4086E77C5E993FDB90a406285d00111a974F877a': {'name': 'Blockchain Brawlers Token', 'symbol': 'BRWL'},
                '0x820802Fa8a99901F52e39acD21177b0BE6EE2974': {'name': 'EUROe Stablecoin', 'symbol': 'EUROe'},
                '0x1aBaEA1f7C830bD89Acc67eC4af516284b1bC33c': {'name': 'EURC', 'symbol': 'EURC'},
                '0x0B4AC2BA3D4924C9A06D9C1d08D7867059A39cC1': {'name': 'Realio Network Utility Token exchangable', 'symbol': 'xRIO'},
                '0x4Bfe661F6901dDa7Cfe02811E89a4c7e10977eFe': {'name': 'Modular L2 Protocol', 'symbol': 'MODUL2'},
                '0x75231F58b43240C9718Dd58B4967c5114342a86c': {'name': 'OKB', 'symbol': 'OKB'},
                '0xeA60CD69F2B9fD6Eb067bdDBbF86a5BdeFfbBc55': {'name': 'Wecan', 'symbol': 'WECAN'},
                '0x00c8555542158Fff0FEb892c8e000a8D1831762C': {'name': 'MORI', 'symbol': 'MORI'},
                '0xbbAec992fc2d637151dAF40451f160bF85f3C8C1': {'name': 'USD Mapped Token', 'symbol': 'USDM'}
            }

            for key, sub_map in proxy_contracts.items():
                if key == str(token_address):
                    name_result = sub_map['name']
                    symbol_result = sub_map['symbol']
                    redis_client.set(key_token_name, name_result)
                    redis_client.set(key_token_symbol_name, symbol_result)
                    return name_result, symbol_result
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


def _parse_row(redis_client, web3, router_codec, etherscan_api_key, row: Row):

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
            token0_address, token1_address = _get_pool_tokens_addresses(redis_client, pool_address=pool_address, pool_contract=pool_contract)

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
                    row_dict['token_in_name'], row_dict['token_in_symbol'] = _get_token_name(redis_client, web3, etherscan_api_key, token0_address)
                    row_dict['token_out_address'] = str(token1_address)
                    row_dict['token_out_name'], row_dict['token_out_symbol'] = _get_token_name(redis_client, web3, etherscan_api_key, token1_address)
                # Otherwise pool token0 is OUTPUT, pool token1 is INPUT
                else:
                    row_dict['token_in_address'] = str(token1_address)
                    row_dict['token_in_name'], row_dict['token_in_symbol'] = _get_token_name(redis_client, web3, etherscan_api_key, token1_address)
                    row_dict['token_out_address'] = str(token0_address)
                    row_dict['token_out_name'], row_dict['token_out_symbol'] = _get_token_name(redis_client, web3, etherscan_api_key, token0_address)

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
                    row_dict['token_in_name'], row_dict['token_in_symbol'] = _get_token_name(redis_client, web3, etherscan_api_key, token0_address)
                    row_dict['token_out_address'] = str(token1_address)
                    row_dict['token_out_name'], row_dict['token_out_symbol'] = _get_token_name(redis_client, web3, etherscan_api_key, token1_address)
                # Otherwise pool token0 is OUTPUT, pool token1 is INPUT
                else:
                    row_dict['token_in_address'] = str(token1_address)
                    row_dict['token_in_name'], row_dict['token_in_symbol'] = _get_token_name(redis_client, web3, etherscan_api_key, token1_address)
                    row_dict['token_out_address'] = str(token0_address)
                    row_dict['token_out_name'], row_dict['token_out_symbol'] = _get_token_name(redis_client, web3, etherscan_api_key, token0_address)
        except NoABIEventsFound:
            callback_msg = 'POOL_ADDRESS_SRC_CODE_NOT_VERIFIED_BY_ETHERSCAN_API_EXC'
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
        yield _parse_row(redis_client, web3, router_codec, os.getenv("ETHERSCAN_API_KEY"), row)

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

    # result.show(150, truncate=False)

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
