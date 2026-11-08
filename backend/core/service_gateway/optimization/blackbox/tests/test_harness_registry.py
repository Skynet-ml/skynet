"""Tests for the harness registry: discovery, order, and launch output that matches the pre-registry catalog."""

from __future__ import annotations

import hashlib
import itertools
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from .. import harnesses
from ..harness import (
    _CATALOG,
    _PARSER_NAMES,
    _PINNED_HARNESSES,
    GatewayConfig,
    build_launch,
    launch_payload,
    pi_editor_launch,
    pinned_harness_check,
)
from ..harness_bridge import PARSERS
from ..harnesses import REGISTRY, HarnessSpec, register

_GATEWAYS = {
    "gw": GatewayConfig(url="https://gw.example/v1", api_key="secret-key"),
    "or": GatewayConfig(url="https://openrouter.ai/api/v1", api_key="secret-key"),
}
_EFFORTS = [None, "high", "none", "max"]
_OVERRIDES: dict[str, dict[str, str]] = {
    "plain": {},
    "override": {"install_command": "my-install {model}", "run_command": "my-run {gateway_url}"},
}
# The golden digests predate the registry, so they cover only these harnesses.
_PRE_REGISTRY = ("pi", "codex", "opencode", "prime")
_BUILT_INS = (*_PRE_REGISTRY, "aider", "antigravity_sdk", "cline")
# sha256 of every launch payload, captured from staging 0e1dd97d5 before the
# catalog moved into the registry. A changed digest means a changed launch.
_GOLDEN = {
    "pi-gw-None-open-plain": "37ff876cbbb6c7256a95ba4d71735b7ae60ff970971ecc1f7dd6adf35a9c9486",
    "pi-gw-None-open-override": "198c0226157dc5123d4ed7e9750eeb14ca6f3113f668f445109bef3655f4bd33",
    "pi-gw-None-protected-plain": "8843536a8e09da511e4c58a931abfbc860f280d057fcfae98c9bb96761874c05",
    "pi-gw-None-protected-override": "c16e53337d5d3c2370ff989dd4ce8d2da415461265f2db2cf1aac4a32df9e8f6",
    "pi-gw-high-open-plain": "9a1bd8a7016ce9648569c511883c1d339371033725d10a3ff92edcf3df2a0b5a",
    "pi-gw-high-open-override": "a7ba0f24a163f1326fc08baf4ae6f047e26eff0d16fdba1141489569cdf355e0",
    "pi-gw-high-protected-plain": "7b297aa0bd370631d4074bb8782b2abada2c9f54b42773bafee63ba9b07b1f03",
    "pi-gw-high-protected-override": "045be1b4507d2c35b9ebd65d408fa6d46c6e8d2be67077f41672148f888b5350",
    "pi-gw-none-open-plain": "856bc599c50f6391858a5b7d18c14197c1de3de15a32ebe047f1e601bcbff8d0",
    "pi-gw-none-open-override": "2dd56a092f794bd152369267de797ec993e13655016246e89e2f1e68cd97db9e",
    "pi-gw-none-protected-plain": "fa6a65996f27d18641bc36de2fea7f6ba201a3e8d06f1efe471007b7645590aa",
    "pi-gw-none-protected-override": "2cbf8f460bfe0e90577de8e1f9aa46bf48e34d28c2fd06a2c5bf34d191a68fea",
    "pi-gw-max-open-plain": "c1ffd3cd3d0916301fd01b3854af7528b4cdc6327e8474b8b25e54b0563df820",
    "pi-gw-max-open-override": "00503ba1dae3b299386e3f38725d44698e650a1a9382a238901ea5e1e262e898",
    "pi-gw-max-protected-plain": "5cef79d69b560b8e1ae2533b6762434c5b7e47d1bf0e315287f9cab288213ae0",
    "pi-gw-max-protected-override": "3b036deb9e0c49f98e4cea41d0d1090267d30cf3865e4aef4075f75735375331",
    "pi-or-None-open-plain": "db1daba60138a2f7bf500d51b47beb9212ad0fb9dbf2f3e02739aef8ca3ae1e2",
    "pi-or-None-open-override": "8a2e84dab44e74b94e848860b1913d1d504606108a35ba67f42e76cbca532e68",
    "pi-or-None-protected-plain": "a7cfe100a130bbd6376a54611ce952cadcbcbb8d0825dfd9058cc37c58aa89dc",
    "pi-or-None-protected-override": "b2dac42685fc98404c4670d4f45b776c9b84dba44730bc5ca5913ec39abb19f9",
    "pi-or-high-open-plain": "4f46065fbbfda9a33a43a93ac79d3eea28859bbc866bc62173aefcd294111038",
    "pi-or-high-open-override": "bb33936d9210a04e60184aa4d8721bd3fe519437a08537576c159e893f3b0869",
    "pi-or-high-protected-plain": "ab4fcdf69affc3bc89ba0fd41f6310b8ecdf5a96d526fe97fdd20eaf2efc4589",
    "pi-or-high-protected-override": "39d21944704d310deadd258d413ae46953d9cc704f91b91d0768c73c06c96a82",
    "pi-or-none-open-plain": "e432c47dcb60fc28be1c58feedcee5a49a28a2d9177cf56d68c3dd845cf47991",
    "pi-or-none-open-override": "4548bdbf01c0e42b0cfe94593fdba3b985de5fb09e9f417de172e69af6377f2a",
    "pi-or-none-protected-plain": "4edabb4a969d88833538e5e621978089e4ed4a847e3ade53796c792b2850ceea",
    "pi-or-none-protected-override": "5d1b599f4e4a19aad16e90b0b323e7c3b3ce8876488a656d651d4475fbad2870",
    "pi-or-max-open-plain": "3ba88e528c12cb0ba3c5b2343665dec3b90c5177dd5c8b70a0e5209819df1c90",
    "pi-or-max-open-override": "dea78c5fac8b05bdf54ac5dbc12d22720093bf94989c8bcad792dc1543eacfe7",
    "pi-or-max-protected-plain": "14dfd9adf23ac913aa1e6ac7050f85a073525c5c1977bc6fc8362d44d07d78b6",
    "pi-or-max-protected-override": "defb3de98950476b59967141aa264a14f18bd1cc21e038d361b30bc504409c1d",
    "codex-gw-None-open-plain": "8444ad6fb9744dec13a94371aa18c2b0c317a2c712df30086c3accdff4d25cc3",
    "codex-gw-None-open-override": "9062d156e0f09e2b04774ab5158eb7bc7fcb7407d600dbba9e4e1fbb65c7957f",
    "codex-gw-None-protected-plain": "f84b6b54bf3b918d07aebb4c257bb21952e07cdfac68c764b6f3100d2e8a2e36",
    "codex-gw-None-protected-override": "cc3a8266e42ea4ea09735b03518e9205dc4610edb84128ec97bf392a25515ada",
    "codex-gw-high-open-plain": "f0b09f2a5091627daa640f1ef8d91e62c990636e34824b9a516a3831c7e41abb",
    "codex-gw-high-open-override": "7deb64141277542605f68ae326f27d581881e3a0389d4a8596dbf3d3752faf9d",
    "codex-gw-high-protected-plain": "5b68096bb572b8620253193f338a4964591ba7c37f8f22d2732243def79e4fd2",
    "codex-gw-high-protected-override": "244971542553081d542823440dd9a215d99560b0168d9666df51206805e18cdf",
    "codex-gw-none-open-plain": "98a4e41c3ddde09e1bf328f46769219c1ba3ea7c7327e213ad331b584e372e2b",
    "codex-gw-none-open-override": "50f28f3796ff597b3efe6e12747f5fe02968b01b0ba83cf26ae901b6df5cc4d3",
    "codex-gw-none-protected-plain": "c7f120e0307fc0b239699380149dd87f1a3b4b6bfd92e546f37b224873311397",
    "codex-gw-none-protected-override": "9a48ae51a1aa8bb73a111822d47004cbd4903b2825e264dbad29224b1d519a80",
    "codex-gw-max-open-plain": "11119bdd6e984a4bc314ff2876bf142c59a1f1b482877d668d4c75c6ff6c32fd",
    "codex-gw-max-open-override": "cf556445d04fde919244fca807a20114abbbce23cd19f3dc5f02369f633a60d2",
    "codex-gw-max-protected-plain": "ca42ff4ce987a4f90ce3b0a007f2adff4b3220853bba5fadd40fdea7c212830b",
    "codex-gw-max-protected-override": "86484c4647755c39ab54b8b288e8a9a74dfc1272de19dc4866f35470109d26e9",
    "codex-or-None-open-plain": "804ce680161abcd395224d6d9fcbbd82a793fe1fdf79d1d885be883d68c62947",
    "codex-or-None-open-override": "421dc209b2dcf07e9f487ddbba713d6187a27088f69089fa1143dfeb8ceb5914",
    "codex-or-None-protected-plain": "cf06de07c8c93a80f98c2f6590afe95378bedb0c5b601e67d66056015a014bd1",
    "codex-or-None-protected-override": "bed043d8e99ed8dda88d42d94fcd0d483f03a2c8fb0f03eeefa7e32ebdb91a07",
    "codex-or-high-open-plain": "5f08bf0fd55efaf46b6bc9b9ef5f7d698551dd60c2285f7a7f04e5c244f40403",
    "codex-or-high-open-override": "8ce27bb9010c9356ace1ba41cdd2725c4e0565f9ff2ee874bfa915361cb79fd8",
    "codex-or-high-protected-plain": "2b3ff91683523281fdabf6f832eb635f74a981d896423419df88348257102c39",
    "codex-or-high-protected-override": "6586a0c74cae05e5eeac50b403ab18d52443846ce4fe374fcfc86a7337c1551c",
    "codex-or-none-open-plain": "6c825f093b1b342263693828674bb46d1ccc442fd43b4f527bd64606059d6c86",
    "codex-or-none-open-override": "be015a5e254e6adda953a906d6eef19b85717452666a9e26bed8de2077b72113",
    "codex-or-none-protected-plain": "914c2cc2ccaf2428e07a88ecc110d4255bfb6ace9f0669c095dcd10610fd93d2",
    "codex-or-none-protected-override": "b1a0e3f9c0967b7ca9b8f37c1cdf765c9a3b030c272028cd404a74c9de333f05",
    "codex-or-max-open-plain": "cf27dcf6421c8abf03d0afc751d99fd2f95602ad13d014739f9d5a7367ffa94f",
    "codex-or-max-open-override": "091d7b73b88e9371eab2206abdba1b61bcfec4670d4a7f374814a865cdfd37ab",
    "codex-or-max-protected-plain": "adc33d19bb7259fb2e49f65a864a5c92f151b4cccb76006fe3f16e8ba0de5fab",
    "codex-or-max-protected-override": "198072b3c681a5452f698277939df1131e1f92c40aefb433730ec9ac394127ea",
    "opencode-gw-None-open-plain": "6882148e2d0e7d18b269ff2d20b551e1ac3072141c9c9de0c06ec42698cfce24",
    "opencode-gw-None-open-override": "69fbaa7c2b485ec69875154009ed927cf59838e86ba7df062875d47934360417",
    "opencode-gw-None-protected-plain": "8089eb8e3cacbe56d5abe39bec29a06f4e3f507bfac91832ec8f8bd9243ec398",
    "opencode-gw-None-protected-override": "1cd889017eb258a9fd43918a3c3f650845ce0bdae603f7d1c3ddb3a17b28e5b1",
    "opencode-gw-high-open-plain": "e6e41a306418340e093c10e570b508f3588fe90d88c6e28d402250163f26bbcb",
    "opencode-gw-high-open-override": "0b4e98104f9e457fabb4ec9c43e08ed2553897057a8f243fc02903694685ace6",
    "opencode-gw-high-protected-plain": "bc0e0cc40a47930242e75a1af246f3d4450f40fbde174531827920e065496880",
    "opencode-gw-high-protected-override": "fda511d703bcef4d2d7f79807cbfa3166af1e86b0950af7dd6ece4242012c3aa",
    "opencode-gw-none-open-plain": "1a3fba8e2df8e602f7100afaaaaaef74b3cbcd2b0e12b04cc0ed990ba2a10b2b",
    "opencode-gw-none-open-override": "e4fbe9cdc7f8c91b042c4b8101cf43ba6103673c1f851ce5b9440c59cf3d8006",
    "opencode-gw-none-protected-plain": "946d03e42450fac8542a144e9672b85950b55ef3a06cefbdce33d0e0dc364655",
    "opencode-gw-none-protected-override": "8d62780c92a87d20b7c4fe3bb575e8ad93685d5b4c837eff8605d073c51c85ea",
    "opencode-gw-max-open-plain": "8005be67316007419bbb07eb69d280df58849960dd18317fb4f6fb7012ecdc90",
    "opencode-gw-max-open-override": "61a36f6d2b21020f3758fc38d9b039ab4b8515b6944c38d602f1bd391262c1d1",
    "opencode-gw-max-protected-plain": "2253fde2b85f2a834cffdbdec571a3d33a17e307c7f9263dfc474acb08103021",
    "opencode-gw-max-protected-override": "6735013ade2d947b8dcc7b0dc94a784b63f428290c695c12464335e20b2366f5",
    "opencode-or-None-open-plain": "e79eba44fe3e6ad8d24e1739a995aa73c62cd2f3a9ae800bae6ff82caedefff1",
    "opencode-or-None-open-override": "208bd950031cca782328baeb3c5ea577bc029c90e94dbcd8628060406eff0754",
    "opencode-or-None-protected-plain": "1ca588547bb5231b187713b67c85cf06f2c77a0a3d62015e61bb39044b010691",
    "opencode-or-None-protected-override": "79d8500373f66ac0dcb0f55767bbe49133d1988c6fdb560197865e202ba9173f",
    "opencode-or-high-open-plain": "e09365da5ef402a94f7222e956e1d230521cd0b109eb8d83409f28a2fa996ffd",
    "opencode-or-high-open-override": "b9d09234f6cba3c3a754058be88fda94798b71f61f14d827729c18c121eb94f8",
    "opencode-or-high-protected-plain": "78f42dac4338b206c0c3a5f950157e4e601ed5b2fb861af3e6e91af73c7fdde2",
    "opencode-or-high-protected-override": "34bda8d6c68afea068033398a272293ca2371da14f8219f6a4ae18cb250fd3ec",
    "opencode-or-none-open-plain": "1954f9afc5dc6d42df5082f9332586604a9e228b9d2196335da568026d38afe1",
    "opencode-or-none-open-override": "b424cb6b3676ad4ae68086babc27db1826a0a167e36e18e52b1b2e8c44e4dd77",
    "opencode-or-none-protected-plain": "11d247b0ef0acb288e0e1a080c7d9da614c8659926849332ceb1c01202a0a759",
    "opencode-or-none-protected-override": "978463846086fa9cd069f43eb3f919e57293ddd6a4c25863065fe55429a32033",
    "opencode-or-max-open-plain": "b4798f9a3e474afd74aac0bdd5914c1939887d39a4cdb7598100b875826afbdc",
    "opencode-or-max-open-override": "543699b64001798e1f4bdf6173c4d2ed39eb06e00206b973ff6f3d9a178250bf",
    "opencode-or-max-protected-plain": "ac0b2fc66226ee3e10a575a59261673d47a43848d2aa66642434278dd924315b",
    "opencode-or-max-protected-override": "611ab9d5b8daba1879fac7f558828a920e93b8cb701182c117520e9f88ab915d",
    "prime-gw-None-open-plain": "bff13930945fc6485bd32f7db934af28c599e862a2f5c312a96bcdc4f009d3d2",
    "prime-gw-None-open-override": "c3f1bcb45bf262c32b52e21a7187cba1ce1d8a2da41e4d6d2799eed14905ce20",
    "prime-gw-None-protected-plain": "6e8a4f36ab165232c53e68d283b0dde598a611a51889cce00b42eab7d770aeb3",
    "prime-gw-None-protected-override": "e9231f074a841cb9499e8b31de67c4b49024ef148415c31f578177718b4f5beb",
    "prime-gw-high-open-plain": "ac73d625c7c553ddd65684eec1a668072ea72b5aecdf273df2b680a333e0b4f1",
    "prime-gw-high-open-override": "50a7197f0a197f33b56ddee2ce2b2543f4a4a854e9e9c0164079391b4f2506d3",
    "prime-gw-high-protected-plain": "484feb05d59a6639cd375937e8f03c6a0bffe0732359e62a33fea3565f1662ec",
    "prime-gw-high-protected-override": "05bbd76f3bd2fa78fc6d86e357b62e2699f9c6e1fcdee510da4147ea3919984e",
    "prime-gw-none-open-plain": "b3dbcf5209d21cf0b7c058c5be55a43423eec68c057bdf2cbe4d5ff4bf1897fb",
    "prime-gw-none-open-override": "bf5409fc0e8fe95aad1464cb3156a002605fa275967a5a07ac421b11eff49215",
    "prime-gw-none-protected-plain": "571649bbc5e6852c7497f47e7df2031b0c5d8d8a6274010f928f4ecf5149b2eb",
    "prime-gw-none-protected-override": "30167cc6f7ce26bef77c0f9bcfff043b40720e1548559101b1712067a71f699f",
    "prime-gw-max-open-plain": "39f0d4ef5197b71b62754180a1a6d3dc3a9c0667769349745cc3a5c0224be47a",
    "prime-gw-max-open-override": "61b592e915aa902508b7804e24a33e618f04d8ec3a4c565c900ff6e799539ec7",
    "prime-gw-max-protected-plain": "8a20d37b8826fe269c62087ae28dda39c3778596348138e162cbd3c831c8fb8c",
    "prime-gw-max-protected-override": "fb2ad6e2593ae929ea7ac7ee93cb721e48af1d5c2313d21f09c04d7a174e9040",
    "prime-or-None-open-plain": "db3c343c8f8aad7236cecaf061dd65d88efdc19c2dadffae3966144c71d98945",
    "prime-or-None-open-override": "abecba6f06031e93ec760d61c527b8ed405a53e4c6ba1d02be18703687787e5d",
    "prime-or-None-protected-plain": "d5309520183186347cc3c4a0db5b02ef6dbed13d1faeb5e7eced704f2a485dd6",
    "prime-or-None-protected-override": "3ec0b60aedc7e83022c2ddbb89f1dfab86c253fd2971b7da2f35a5689d9d4692",
    "prime-or-high-open-plain": "99850c56383a83706ad47c31771a700b5c5127041eeef99896a17a74fed2df3d",
    "prime-or-high-open-override": "74516aa74f79a0ce0e98138333e16ea9124386ebe567e9119fab77da598f0e8a",
    "prime-or-high-protected-plain": "972168ec0e5d0bb51d462bf3f44eecaad37dbf80c7e8483cc95cba12b4117ce4",
    "prime-or-high-protected-override": "09c9b37b397bd1ec7b62384dcb9de01dcd34f7e8c84637315bdf42d48e285158",
    "prime-or-none-open-plain": "d5323b17cae4b117e4aad8f132cf2232a7ce4244cca680b8449c865752c3ee26",
    "prime-or-none-open-override": "eea6a15a435677269f1b33f0cff2a1048fe27e902a6fd46a77b0011e6b2e3887",
    "prime-or-none-protected-plain": "5b6bfad236bc034f317774fb027eec45a9bbcf8b3d66386077014b249fa2e5e8",
    "prime-or-none-protected-override": "f3db645945a51af528ad277bca48c73748543c4cb805d2a568d306812d7547da",
    "prime-or-max-open-plain": "fea4392ac3ccef79414541808ab51c094ddbce1b3ee38a071797c70d2ab36d24",
    "prime-or-max-open-override": "56c27d2541cd67ca081dbb1672ed047b745647742abeeeae033d4f49ee7a25ce",
    "prime-or-max-protected-plain": "32887e762ab9897d855a971e7fa2d56f4553c97ef071f3dc885fda6367335b84",
    "prime-or-max-protected-override": "b1f9c46400afc23b7f52fbb6ee398f74fe01f308e285b9f5aff71fed0ca380c1",
    "pi_editor-gw-None-open": "4b7e5d95f6365ff3798818fa271ba4116d5693cd658b6f40df974e2c0a60a175",
    "pi_editor-gw-None-protected": "b1df47a16ed5de44a169bf121074c24c9c80e353e6a8cea489930904c1fafbbc",
    "pi_editor-gw-high-open": "cabb657714bdcd8e680e0f83f2743bb6b1d79bf96a19954fb81ab32eea8a9502",
    "pi_editor-gw-high-protected": "fc964d14b20d1f35727e4a018171e0dbbc73ab517e2b4dcf8e9f809d351fb199",
    "pi_editor-gw-none-open": "961b82c713f977c9324007c1e5b71a282a07835b949adf90f19a5253883c72f1",
    "pi_editor-gw-none-protected": "551222ae38dc899acb32fa86ba63e29b55865bb5dc32160f50a91075404e48d4",
    "pi_editor-gw-max-open": "dd946c322aad1724308e4d724ad089b064b5425e72434210d0da163718b2da3d",
    "pi_editor-gw-max-protected": "d989ebe73184cd1974c845e94e1c4b081a6ecd10b5ae0fb14de6412dd881196c",
    "pi_editor-or-None-open": "f84f9e97f7c7fcee08f0825a7220c07809e84a5a50039a73d9269b5c82a04cfb",
    "pi_editor-or-None-protected": "66f430eb035707b19cd29aa7e70274c8535f6bef49716dfa2072cde4d0767082",
    "pi_editor-or-high-open": "2b530381af7f331da02addb91d17c33c4436ae872d8c4125db033ef53f2b46bf",
    "pi_editor-or-high-protected": "5a00e4b496e5cae97829c375a88e72cd96068e18e418384df3b66fb9d52c0b2b",
    "pi_editor-or-none-open": "6d80960e5e46b49d92a02d1134e2b3c147f2fe8f69537b7d54525026f2df8b3a",
    "pi_editor-or-none-protected": "0c899171f5af847a12306d2f927253b12a738c7d92bb3da13e3c6db3691d97f1",
    "pi_editor-or-max-open": "ccdf57dba472c72a2592ea078085031bc621e057d4faa39a69d3e1fb672e6ed4",
    "pi_editor-or-max-protected": "493cf83c9589012c4d6f24a57d8c5e3cc4b4bea3b64c2d4e28911d1b7e3efc14",
    "custom-None-open": "4664eeaf1f92b1d21ee528a93171dc3448bb108570531206d4f1798fa7784b70",
    "custom-None-protected": "281061ded5187d8ea1d4a0a0d024671d7c0231efcc2ba17332e440c816b48f49",
    "custom-high-open": "99500c5d07e0d46ac0625b7fffdc71dd67d0c0b7fa80e669d9bb54d0e0808f0a",
    "custom-high-protected": "9faf42a9c2ac75ef0f28af116dbeb76b9f9113bc141372aabca2e12eb0143a4d",
    "custom-none-open": "ef85cd97ff854aeb75415d0e82708f7fe0b4a9c6f9510d427e11d5dec1800434",
    "custom-none-protected": "9fac32d3f2b47f3326cdb979bf8b763a764780c4b8f9eeecfeb141c6e4762633",
    "custom-max-open": "35d123de0ff154907264054fe4876ea5f20ad78ec1ee2797b6c6b06c515c6293",
    "custom-max-protected": "c556adb1e023cc1ed31a7f8065c2c91ef40885e88172e85e421529d44b133cdc",
}


def _digest(payload: dict[str, Any]) -> str:
    """Hash a launch payload the way the golden digests were captured.

    Args:
        payload: JSON-ready launch payload.

    Returns:
        The hex sha256 digest.
    """
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _current_digests() -> dict[str, str]:
    """Build every golden case with the current code and hash each payload.

    Returns:
        The digests, keyed like :data:`_GOLDEN`.
    """
    out: dict[str, str] = {}
    for h, (gname, gw), effort, protected, (oname, extra) in itertools.product(
        _PRE_REGISTRY, _GATEWAYS.items(), _EFFORTS, [False, True], _OVERRIDES.items()
    ):
        target = BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=h, model="openrouter/acme/agent-1", **extra)
        payload = launch_payload(build_launch(target, gw, protected=protected, reasoning_effort=effort))
        payload["pinned_check"] = pinned_harness_check(h)
        out[f"{h}-{gname}-{effort}-{'protected' if protected else 'open'}-{oname}"] = _digest(payload)
    for (gname, gw), effort, protected in itertools.product(_GATEWAYS.items(), _EFFORTS, [False, True]):
        launch = pi_editor_launch(
            "openrouter/acme/agent-1", gw, headers={"X-Tag": "t"}, protected=protected, reasoning_effort=effort
        )
        out[f"pi_editor-{gname}-{effort}-{'protected' if protected else 'open'}"] = _digest(launch_payload(launch))
    for effort, protected in itertools.product(_EFFORTS, [False, True]):
        target = BlackboxTarget(
            kind=BLACKBOX_TARGET_AGENT,
            harness="custom",
            model="m",
            install_command="setup {model}",
            run_command="agent {model} {gateway_url} {api_key} {prompt_file} {answer_file}",
        )
        launch = build_launch(target, _GATEWAYS["gw"], protected=protected, reasoning_effort=effort)
        out[f"custom-{effort}-{'protected' if protected else 'open'}"] = _digest(launch_payload(launch))
    return out


def test_launch_output_matches_the_pre_registry_catalog() -> None:
    """Keep argv, env, files and the pinned check byte-identical for every built-in harness."""
    current = _current_digests()
    assert set(current) == set(_GOLDEN)
    assert {key for key in _GOLDEN if current[key] != _GOLDEN[key]} == set()


def test_registry_keeps_the_catalog_order() -> None:
    """Keep the built-in order that the unknown-harness error text lists."""
    assert tuple(REGISTRY) == _BUILT_INS
    assert tuple(_CATALOG) == _BUILT_INS
    assert {h: _PINNED_HARNESSES[h] for h in _PINNED_HARNESSES} == {
        "pi": ("pi", "0.87.1"),
        "codex": ("codex", "0.153.0"),
        "opencode": ("opencode", "1.18.27"),
        "prime": ("prime-agent", "0.9.7"),
        "aider": ("aider", "0.86.2"),
        "antigravity_sdk": (
            "uv run --quiet --offline --no-project --python '>=3.12,<3.15' --with google-antigravity==0.1.20"
            " --with cryptography==46.0.7 --with fastapi==0.136.3 python -c"
            """ 'import importlib.metadata as m; print(m.version("google-antigravity"))'""",
            "0.1.20",
        ),
        "cline": ("cline", "3.0.74"),
    }


def test_model_harness_ids_match_the_registry() -> None:
    """Fail when a registered harness is missing from the request model's allowed ids."""
    assert (*REGISTRY, "custom") == BLACKBOX_HARNESSES


@pytest.mark.parametrize("spec", list(REGISTRY.values()), ids=list(REGISTRY))
def test_spec_output_format_is_the_launch_parser(spec: HarnessSpec) -> None:
    """Make every spec name a bridge parser, and the parser its launch really uses."""
    launch = spec.launch("acme/agent-1", _GATEWAYS["gw"])
    assert spec.output_format in PARSERS
    assert _PARSER_NAMES[launch.parse_output] == spec.output_format


def test_register_rejects_a_duplicate_id() -> None:
    """Refuse a second spec with an id that is already registered."""
    existing = REGISTRY["pi"]
    with pytest.raises(ValueError, match="already registered"):
        register(HarnessSpec(id="pi", launch=existing.launch, binary="pi", version="0", output_format="pi"))
    assert REGISTRY["pi"] is existing


_TEMP_MODULE = """\"\"\"A test-only harness.\"\"\"

from ..harness_bridge import parse_plain_output
from . import HarnessSpec, register
from .base import GatewayConfig, HarnessLaunch, _base_env


def _launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    \"\"\"Build the test launch.\"\"\"
    return HarnessLaunch(
        instructions_file="AGENTS.md",
        run_command="tempcli run",
        env=_base_env(model, gateway),
        parse_output=parse_plain_output,
    )


register(HarnessSpec(id="tempcli", launch=_launch, binary="tempcli", version="1.2.3", output_format="plain"))
"""


def test_a_new_module_registers_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Add a harness from its own module, with no edit to any shared file."""
    (tmp_path / "tempcli.py").write_text(_TEMP_MODULE, encoding="utf-8")
    monkeypatch.setattr(harnesses, "__path__", [*harnesses.__path__, str(tmp_path)])
    module_name = f"{harnesses.__name__}.tempcli"
    try:
        harnesses.discover()
        assert "tempcli" in REGISTRY
        assert list(REGISTRY)[-1] == "tempcli"
        assert _CATALOG["tempcli"] is REGISTRY["tempcli"].launch
        assert pinned_harness_check("tempcli") is not None
        target = BlackboxTarget.model_construct(kind=BLACKBOX_TARGET_AGENT, harness="tempcli", model="acme/agent-1")
        launch = build_launch(target, _GATEWAYS["gw"])
        assert launch.run_command == "tempcli run"
        assert launch_payload(launch)["output_format"] == "plain"
    finally:
        REGISTRY.pop("tempcli", None)
        sys.modules.pop(module_name, None)
    assert "tempcli" not in REGISTRY


def test_unknown_harness_still_raises() -> None:
    """Keep the error for an id that no module registers."""
    target = BlackboxTarget.model_construct(kind=BLACKBOX_TARGET_AGENT, harness="nope", model="acme/agent-1")
    with pytest.raises(ServiceError, match="Unknown harness 'nope'"):
        build_launch(target, _GATEWAYS["gw"])
