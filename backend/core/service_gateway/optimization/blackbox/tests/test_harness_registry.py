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
from ..harnesses.deerflow import DEERFLOW_REF
from ..harnesses.deerflow import SPEC as DEERFLOW_SPEC

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
_BUILT_INS = (
    "pi",
    "codex",
    "opencode",
    "opencode_v2",
    "prime",
    "aider",
    "openhands",
    "openhands_sdk",
    "antigravity_sdk",
    "goose",
    "hermes",
    "eve",
    "deerflow",
    "cline",
    "code_puppy",
)
# sha256 of every launch payload, captured from staging 0e1dd97d5 before the
# catalog moved into the registry. A changed digest means a changed launch.
# SKY-529 changed the codex digests: Codex turns off web_search and multi_agent,
# in config.toml and again as -c overrides on the command line.
# SKY-595 changed every protected digest: the relay rewrite reads and writes
# the config as UTF-8.
_GOLDEN = {
    "pi-gw-None-open-plain": "37ff876cbbb6c7256a95ba4d71735b7ae60ff970971ecc1f7dd6adf35a9c9486",
    "pi-gw-None-open-override": "198c0226157dc5123d4ed7e9750eeb14ca6f3113f668f445109bef3655f4bd33",
    "pi-gw-None-protected-plain": "6673225312e820e7a79c00d5416f54b3e54d751d2e9ea713041afe64ea4b5c8c",
    "pi-gw-None-protected-override": "b899332ee09ba86773b71e3519723caae7545096140466a17f6851e74b9432bf",
    "pi-gw-high-open-plain": "9a1bd8a7016ce9648569c511883c1d339371033725d10a3ff92edcf3df2a0b5a",
    "pi-gw-high-open-override": "a7ba0f24a163f1326fc08baf4ae6f047e26eff0d16fdba1141489569cdf355e0",
    "pi-gw-high-protected-plain": "a01561ab554505d414132c306f6d11fbeb037147ba0c8da1054e80600e9b464f",
    "pi-gw-high-protected-override": "e9791aad39a5ab9ed64e4a66e47a4bb78b3a12efb7c1a7650b15a5365b930cb7",
    "pi-gw-none-open-plain": "856bc599c50f6391858a5b7d18c14197c1de3de15a32ebe047f1e601bcbff8d0",
    "pi-gw-none-open-override": "2dd56a092f794bd152369267de797ec993e13655016246e89e2f1e68cd97db9e",
    "pi-gw-none-protected-plain": "fb5b95d9c67ccfde4e7f5efd14a994f1881a157b1a365e5c84a499695fd825bb",
    "pi-gw-none-protected-override": "a1698f16c22f7b087d8edb79c7dbf2bd86ea90269674bacbcb4e5d78529c4218",
    "pi-gw-max-open-plain": "c1ffd3cd3d0916301fd01b3854af7528b4cdc6327e8474b8b25e54b0563df820",
    "pi-gw-max-open-override": "00503ba1dae3b299386e3f38725d44698e650a1a9382a238901ea5e1e262e898",
    "pi-gw-max-protected-plain": "91d60cc38d08cfec98645537d8bcddc3b2627c5272abe89977229fa6292b3b86",
    "pi-gw-max-protected-override": "3579a8c5d1593cf57053b6c566af8c2b997bc5f6d79f458a25e5d541dbf0d32d",
    "pi-or-None-open-plain": "db1daba60138a2f7bf500d51b47beb9212ad0fb9dbf2f3e02739aef8ca3ae1e2",
    "pi-or-None-open-override": "8a2e84dab44e74b94e848860b1913d1d504606108a35ba67f42e76cbca532e68",
    "pi-or-None-protected-plain": "34ac115d6300b27af1d690353157e683528c61bad370d6f6b4b1f97987a1f057",
    "pi-or-None-protected-override": "41e93477833ad8abe2a4c2458abab0b39e6c06378df39922c2daa141c2d5b86b",
    "pi-or-high-open-plain": "4f46065fbbfda9a33a43a93ac79d3eea28859bbc866bc62173aefcd294111038",
    "pi-or-high-open-override": "bb33936d9210a04e60184aa4d8721bd3fe519437a08537576c159e893f3b0869",
    "pi-or-high-protected-plain": "4149a71aec0c91c8656f64a86bb8cda7b6bd05d00620317e7ab58cc502ba0633",
    "pi-or-high-protected-override": "7670a4c70662153ec586342863fd315efdf1342d4b8539ab6edf5f175f0045fa",
    "pi-or-none-open-plain": "e432c47dcb60fc28be1c58feedcee5a49a28a2d9177cf56d68c3dd845cf47991",
    "pi-or-none-open-override": "4548bdbf01c0e42b0cfe94593fdba3b985de5fb09e9f417de172e69af6377f2a",
    "pi-or-none-protected-plain": "b8c2235b1e3c93309078090f9cee9e728cb24d4be5cb9c49d59fbb413844b760",
    "pi-or-none-protected-override": "4e871db9cf44efec8418c3814557e2a73e61899b2eb7fb9523ead2d484bd354b",
    "pi-or-max-open-plain": "3ba88e528c12cb0ba3c5b2343665dec3b90c5177dd5c8b70a0e5209819df1c90",
    "pi-or-max-open-override": "dea78c5fac8b05bdf54ac5dbc12d22720093bf94989c8bcad792dc1543eacfe7",
    "pi-or-max-protected-plain": "4bc319e25ef92810f91f4e540475b3939c8aef9104ca5437f4c811497254afc0",
    "pi-or-max-protected-override": "3708eb82307516eba5e0dcdabae9b5b59ca6aac6e934d4ed20eff821823f7e1a",
    "codex-gw-None-open-plain": "3180f249530e4797ffa83292683fe7628bd6471c01afd5836f1603d1e668ba96",
    "codex-gw-None-open-override": "463c742476d386837aed97d2d995dd305a6d128d907824146bcf4a1815665b5b",
    "codex-gw-None-protected-plain": "93ad3a73bf20dbf422cf147c9c932101c277044bf824ed38efbd543e0c84b3c7",
    "codex-gw-None-protected-override": "f6ff103d763708799eb10708be53a711a9098928e79d749ad66b8713210d9138",
    "codex-gw-high-open-plain": "5c1dea28dbd654ed17bc933d2db300d7153e0cb3d6ceb526cb8261e166d56b97",
    "codex-gw-high-open-override": "6b99acdb8dde5e670e7e5079ce2b17af21f135f35bf4ec95441f8d0a008b709f",
    "codex-gw-high-protected-plain": "c990f873da36e7fb78e29472c594290a378bce14b68892ecf6db0953ecb3b818",
    "codex-gw-high-protected-override": "d89a2d988b6de982625caaf1358c114d5c3c469bef998dc70dfcd349e8008ac7",
    "codex-gw-none-open-plain": "fb7baa9830cab755cbe9053ade7530c26fff74052c3aef96b3793be72e174225",
    "codex-gw-none-open-override": "68c9e7cc444fdac1c5ed1d18f49c61b08a57782f57660c9cf17eb49ca39c0ef2",
    "codex-gw-none-protected-plain": "6b491ff143b28ce6e3ec2ff1264b4e537f399f20326beafa17367b2819a9ec14",
    "codex-gw-none-protected-override": "04c6db6201455e70da4ef8430774b3114d9319d45c6f61938b820627f57206e0",
    "codex-gw-max-open-plain": "d3e48bd8d0db705a1e0daf83af6179e0ecf2b4351c485e459e8cf43f92a28d9f",
    "codex-gw-max-open-override": "d0ea8726bbcb1abc848ff265ac0867aefe9f12b400dd1936578db7fb1a229e2c",
    "codex-gw-max-protected-plain": "37c9fe1e271f45092b549e8c26fd43f1e2935dc3a9c1fb882998812277e63512",
    "codex-gw-max-protected-override": "72a6afbbf4053eddda809ef06081807a26f7ebae14c56786922fb44d1439d053",
    "codex-or-None-open-plain": "8a4aff48e28dd51aabb8e4874824765dce467eb018199d29abf204a284589bd0",
    "codex-or-None-open-override": "7d25985e4038a72bec7be4823700146d5c33f4c9cf362ea4bd0a60c78eb5afc9",
    "codex-or-None-protected-plain": "a4449a27152f906b9bf55096e91c5f51c095767647e5580c461bd654930429df",
    "codex-or-None-protected-override": "a45941280a6b810029afa49f40114b130807b0dbcac2d05dbd35c6e355edb717",
    "codex-or-high-open-plain": "851404c874edc409539abff26c3c2448eb60d8acec330e38e7e899c6e0ca3f7c",
    "codex-or-high-open-override": "fbb0136b325e1d88aef03d4f06f043308e06c627bc6b067b3a834f13dd03fb8f",
    "codex-or-high-protected-plain": "7cf30cc7121529aff56476ede62939686a80dbe3102500509b9522e3b6a21534",
    "codex-or-high-protected-override": "79af978f38ef0e6116cf1ea3886a4abf67e40b81cda8d3357fb6bb66e5bc983a",
    "codex-or-none-open-plain": "8894baa78a9f900e999bedb953edbc149a7417be343716a5067adaa8775c32de",
    "codex-or-none-open-override": "c39e8b183d5f15aeb744a52616a574371b93cdff75183bf4468a17b2423d8ba9",
    "codex-or-none-protected-plain": "ccecd7ff318aea462c359b3f730e8bba5982173a34e7b3e24d22ff15a8cd08b9",
    "codex-or-none-protected-override": "2a3b1c9d18de8e50504fbc698bba72f264f1161194790fbc10ef7218d83052f0",
    "codex-or-max-open-plain": "0c2444a28f9d4985e1b2246713e42614c5950f40773c9d1e454cff29bb8589ea",
    "codex-or-max-open-override": "e493303cec7e7b99cae866a75e8ec5a9db88de02371bd877ecd05de60fbd080f",
    "codex-or-max-protected-plain": "38ac826ade5d6f11ddac15fc0a6e63fdde3d83674b0c7a4ced99ff1c40d12c86",
    "codex-or-max-protected-override": "6f6fa4aba2428109d389596aebd276e8d51d586414f3a6b38f0a42fcc9f11051",
    "opencode-gw-None-open-plain": "6882148e2d0e7d18b269ff2d20b551e1ac3072141c9c9de0c06ec42698cfce24",
    "opencode-gw-None-open-override": "69fbaa7c2b485ec69875154009ed927cf59838e86ba7df062875d47934360417",
    "opencode-gw-None-protected-plain": "06d12a972033e92cb1f9e08eab04f0f377f0880b18e8499856261712bce81740",
    "opencode-gw-None-protected-override": "cfa8639f42555da9c150822857cc62a5ca0bf2185770989e1086840ebd7b6ecc",
    "opencode-gw-high-open-plain": "e6e41a306418340e093c10e570b508f3588fe90d88c6e28d402250163f26bbcb",
    "opencode-gw-high-open-override": "0b4e98104f9e457fabb4ec9c43e08ed2553897057a8f243fc02903694685ace6",
    "opencode-gw-high-protected-plain": "b2d3b5521d3ce91c6df9342300caaf4f2edf13bb843bd5b218b11d5822142a71",
    "opencode-gw-high-protected-override": "a2583ef38addddda6e7d9e0205c343e2df0b852ed9cef2c69e9353d088aa3968",
    "opencode-gw-none-open-plain": "1a3fba8e2df8e602f7100afaaaaaef74b3cbcd2b0e12b04cc0ed990ba2a10b2b",
    "opencode-gw-none-open-override": "e4fbe9cdc7f8c91b042c4b8101cf43ba6103673c1f851ce5b9440c59cf3d8006",
    "opencode-gw-none-protected-plain": "6eff35773584553473439a1a61622417f2ea3371c7d95025ea9f32d7dcd085e2",
    "opencode-gw-none-protected-override": "f02d09cd30f9a07b00115f71f01de4ec98978bd0c943ce66f877918c0fab918a",
    "opencode-gw-max-open-plain": "8005be67316007419bbb07eb69d280df58849960dd18317fb4f6fb7012ecdc90",
    "opencode-gw-max-open-override": "61a36f6d2b21020f3758fc38d9b039ab4b8515b6944c38d602f1bd391262c1d1",
    "opencode-gw-max-protected-plain": "270ada8df58ca4b140b6998db7ff4b28d4e1206842a96a18e02cf1747491cdc5",
    "opencode-gw-max-protected-override": "840b017a20ae1bb2dd73a054836835d246c26a72ec8f9c2ca6a5256e661fd3e0",
    "opencode-or-None-open-plain": "e79eba44fe3e6ad8d24e1739a995aa73c62cd2f3a9ae800bae6ff82caedefff1",
    "opencode-or-None-open-override": "208bd950031cca782328baeb3c5ea577bc029c90e94dbcd8628060406eff0754",
    "opencode-or-None-protected-plain": "1f6e4e73fed683f987aca4bbae9d965c25ed30ce906511f632e6aed095fa5b33",
    "opencode-or-None-protected-override": "0dc4cb4540a3d46361fe3945d0cfff24b06188fab0671f2282594630facf7043",
    "opencode-or-high-open-plain": "e09365da5ef402a94f7222e956e1d230521cd0b109eb8d83409f28a2fa996ffd",
    "opencode-or-high-open-override": "b9d09234f6cba3c3a754058be88fda94798b71f61f14d827729c18c121eb94f8",
    "opencode-or-high-protected-plain": "da4f58f9f8afcae648ce9e7e5dd219cfe1683e26c33e096c599daf96695cad47",
    "opencode-or-high-protected-override": "a1c955bb7a17adbeb499526ac6486a4759a241db659becec07f7d14e6803dd1a",
    "opencode-or-none-open-plain": "1954f9afc5dc6d42df5082f9332586604a9e228b9d2196335da568026d38afe1",
    "opencode-or-none-open-override": "b424cb6b3676ad4ae68086babc27db1826a0a167e36e18e52b1b2e8c44e4dd77",
    "opencode-or-none-protected-plain": "1c520731fd2cd28f2dfc31aaba9b4823298785fe5d62256e63bfd3ebc8b52147",
    "opencode-or-none-protected-override": "e4380821b768708e4e781c6ae93e4310725d60b7d4c71dc6225151e44558a6f8",
    "opencode-or-max-open-plain": "b4798f9a3e474afd74aac0bdd5914c1939887d39a4cdb7598100b875826afbdc",
    "opencode-or-max-open-override": "543699b64001798e1f4bdf6173c4d2ed39eb06e00206b973ff6f3d9a178250bf",
    "opencode-or-max-protected-plain": "66beba1785567f4439bdd1375451fd86d892285efcf54dc662aecdd354cdfb73",
    "opencode-or-max-protected-override": "e06b5d96f74c3080c91f26b5e7e886623396848097f3ba70c12c2ccbb9ea3bc7",
    "prime-gw-None-open-plain": "bff13930945fc6485bd32f7db934af28c599e862a2f5c312a96bcdc4f009d3d2",
    "prime-gw-None-open-override": "c3f1bcb45bf262c32b52e21a7187cba1ce1d8a2da41e4d6d2799eed14905ce20",
    "prime-gw-None-protected-plain": "d89422d0f4822e3091e3ed4c6d034831d1853e2521a90a390eba4089fe4a6974",
    "prime-gw-None-protected-override": "0ac2d7cb11e9151e1022f2eeb6052e58a61dc16aea78d799fdd9c48e46bacf6c",
    "prime-gw-high-open-plain": "ac73d625c7c553ddd65684eec1a668072ea72b5aecdf273df2b680a333e0b4f1",
    "prime-gw-high-open-override": "50a7197f0a197f33b56ddee2ce2b2543f4a4a854e9e9c0164079391b4f2506d3",
    "prime-gw-high-protected-plain": "3283caee0d9cdf2aa3e72c13304fef9094506b7ac62a438033d47c6dba7a7c66",
    "prime-gw-high-protected-override": "af9a7d43b1859175a8b0c39a5ea3edf8184565bc5b812ab0aa178507d0a1ef93",
    "prime-gw-none-open-plain": "b3dbcf5209d21cf0b7c058c5be55a43423eec68c057bdf2cbe4d5ff4bf1897fb",
    "prime-gw-none-open-override": "bf5409fc0e8fe95aad1464cb3156a002605fa275967a5a07ac421b11eff49215",
    "prime-gw-none-protected-plain": "5f81b1b62377840512f31d9459a276919b87d265e7818618f63011213a365a18",
    "prime-gw-none-protected-override": "b1a41331d9210eecc22bb2e4865d0b25ceb7dffd25d3bba7e709506148aaf9ee",
    "prime-gw-max-open-plain": "39f0d4ef5197b71b62754180a1a6d3dc3a9c0667769349745cc3a5c0224be47a",
    "prime-gw-max-open-override": "61b592e915aa902508b7804e24a33e618f04d8ec3a4c565c900ff6e799539ec7",
    "prime-gw-max-protected-plain": "05113d0847e725a01fb3c403aa34ad0b8c68f40a29541396a9ce1a6678ad99df",
    "prime-gw-max-protected-override": "4c459302d3320ff5bb14add0131e3846c720cb010bcfd9ff861e00d356bd1524",
    "prime-or-None-open-plain": "db3c343c8f8aad7236cecaf061dd65d88efdc19c2dadffae3966144c71d98945",
    "prime-or-None-open-override": "abecba6f06031e93ec760d61c527b8ed405a53e4c6ba1d02be18703687787e5d",
    "prime-or-None-protected-plain": "7f8baf9403525c0d4e5b64ce13a269a6c4b4eb8779823ed8d7535742fe8c4e09",
    "prime-or-None-protected-override": "f0c260927306048227627d56031f82422017755277296fe5210b47f28b862552",
    "prime-or-high-open-plain": "99850c56383a83706ad47c31771a700b5c5127041eeef99896a17a74fed2df3d",
    "prime-or-high-open-override": "74516aa74f79a0ce0e98138333e16ea9124386ebe567e9119fab77da598f0e8a",
    "prime-or-high-protected-plain": "fb81c9949d901881e6c5413e1a57af8cb8c24d87cd0e8520ace6a11d64b23eff",
    "prime-or-high-protected-override": "cce42a2194d467e8fe71491e4c025d4960328bd6ea3fa22f2686e4a9f6e3a6c1",
    "prime-or-none-open-plain": "d5323b17cae4b117e4aad8f132cf2232a7ce4244cca680b8449c865752c3ee26",
    "prime-or-none-open-override": "eea6a15a435677269f1b33f0cff2a1048fe27e902a6fd46a77b0011e6b2e3887",
    "prime-or-none-protected-plain": "fcbcd1f3b0ef3e9b4b332627df17d754d3ea6b5164299f2cab18f9772073ad96",
    "prime-or-none-protected-override": "3f1e24ee633e5151415303697682d8e2246eeccb0a4cf348cd5d942e0cc2817e",
    "prime-or-max-open-plain": "fea4392ac3ccef79414541808ab51c094ddbce1b3ee38a071797c70d2ab36d24",
    "prime-or-max-open-override": "56c27d2541cd67ca081dbb1672ed047b745647742abeeeae033d4f49ee7a25ce",
    "prime-or-max-protected-plain": "c599e6bc966664e2bd0bcf06a28c5e2440b5abf41493428da644309bb31e675d",
    "prime-or-max-protected-override": "97d9d0629494112573aa28b1c3cdae01b6023cdf8916e69a279ce327e2ffb406",
    "pi_editor-gw-None-open": "4b7e5d95f6365ff3798818fa271ba4116d5693cd658b6f40df974e2c0a60a175",
    "pi_editor-gw-None-protected": "2d736eb83c65867563b67d83127a635467f9280c7d7f93ab01a682d82840fbce",
    "pi_editor-gw-high-open": "cabb657714bdcd8e680e0f83f2743bb6b1d79bf96a19954fb81ab32eea8a9502",
    "pi_editor-gw-high-protected": "2bdc6e315dbb6b37b5cfc978024e291a3f547cda1f2e3324911477aae4136f45",
    "pi_editor-gw-none-open": "961b82c713f977c9324007c1e5b71a282a07835b949adf90f19a5253883c72f1",
    "pi_editor-gw-none-protected": "97e28e27fd272874454744ee3f15dc9ef493c2e6044d885ba280e0dd66f51846",
    "pi_editor-gw-max-open": "dd946c322aad1724308e4d724ad089b064b5425e72434210d0da163718b2da3d",
    "pi_editor-gw-max-protected": "c5db4a310c8baf5ea7292e4df2406adbed738171252a43b4441de05fd9de5c9e",
    "pi_editor-or-None-open": "f84f9e97f7c7fcee08f0825a7220c07809e84a5a50039a73d9269b5c82a04cfb",
    "pi_editor-or-None-protected": "b2067a01b98dea52f068f6f462636703d77a3346f91744f03a9590cfaa9bafe8",
    "pi_editor-or-high-open": "2b530381af7f331da02addb91d17c33c4436ae872d8c4125db033ef53f2b46bf",
    "pi_editor-or-high-protected": "6122888dda46ea37a846b1ca39eb9e8b97bd2b9ba072c3da6e8f0cd06f4c4981",
    "pi_editor-or-none-open": "6d80960e5e46b49d92a02d1134e2b3c147f2fe8f69537b7d54525026f2df8b3a",
    "pi_editor-or-none-protected": "0192ae51f1b0ea9a0fa0bc68448ea52f80c6b474f25df462b37f99e827eb4f19",
    "pi_editor-or-max-open": "ccdf57dba472c72a2592ea078085031bc621e057d4faa39a69d3e1fb672e6ed4",
    "pi_editor-or-max-protected": "58b18445a6686c654a36cb277c44ad3f3cc8c93dee252faac3ab1c282175f2b4",
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
        "opencode_v2": ("opencode2", "v2.2.7"),
        "prime": ("prime-agent", "0.9.7"),
        "aider": ("aider", "0.86.2"),
        "openhands": ("openhands", "1.6.0"),
        "openhands_sdk": (
            "uv run --quiet --offline --no-project --python '>=3.12,<3.15' --with openhands-sdk==1.61.0"
            " --with openhands-tools==1.61.0 --with fastapi==0.136.3 python -c"
            """ 'import importlib.metadata as m; print(m.version("openhands-sdk"))'""",
            "1.61.0",
        ),
        "antigravity_sdk": (
            "uv run --quiet --offline --no-project --python '>=3.12,<3.15' --with google-antigravity==0.1.20"
            " --with cryptography==46.0.7 --with fastapi==0.136.3 python -c"
            """ 'import importlib.metadata as m; print(m.version("google-antigravity"))'""",
            "0.1.20",
        ),
        "goose": ("goose", "1.57.0"),
        "hermes": ("hermes", "v0.19.0"),
        "eve": ("eve", "1.0.0-rc.6"),
        "deerflow": (DEERFLOW_SPEC.binary, DEERFLOW_REF),
        "cline": ("cline", "3.0.74"),
        "code_puppy": ("code-puppy", "0.0.967"),
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
