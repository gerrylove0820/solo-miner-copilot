"""Confirmed payout blocks: coinbase match, baseline, reorg, privacy."""
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from app.blockwatch import (
    BTC_BLOCK_500000,
    BTC_CHECK_HEIGHT,
    BlockWatch,
    ChainSettings,
    JsonRpc,
    RpcError,
    chains_from_env,
    coinbase_addresses,
    payout_matches,
)
from app.main import App
from app.config import load_config
from app.store import Store

PAY = "bc1qexampleexampleexampleexampleexamp"
OTHER = "bc1qotherotherotherotherotherotherox"
BCH_PAY = "bitcoincash:" + "q" + ("p" * 41)


class Node:
    def __init__(self, split=BTC_BLOCK_500000):
        self.split_hash = split
        self.chain = "main"
        self.ibd = False
        self.tip = 0
        self.headers = None
        self.fail = False
        self.by_height = {}
        self.by_hash = {}

    def add(self, height, address=None, *, coinbase=True, second=None, address_list=False):
        block_hash = f"{height:064x}"
        if address is None:
            spk = {"address": OTHER}
        elif address_list:
            spk = {"addresses": [address]}
        else:
            spk = {"address": address}
        vin = {"coinbase": "aa"} if coinbase else {"txid": "zz"}
        txs = [{"vin": [vin], "vout": [{"scriptPubKey": spk}]}]
        if second:
            txs.append({"vin": [{"txid": "yy"}], "vout": [{"scriptPubKey": {"address": second}}]})
        self.by_height[height] = block_hash
        self.by_hash[block_hash] = {"hash": block_hash, "height": height, "tx": txs}
        return block_hash

    def replace(self, height, address):
        block_hash = f"f{height:063x}"
        self.by_height[height] = block_hash
        self.by_hash[block_hash] = {
            "hash": block_hash, "height": height,
            "tx": [{"vin": [{"coinbase": "bb"}], "vout": [{"scriptPubKey": {"address": address}}]}],
        }
        return block_hash

    def getblockchaininfo(self):
        if self.fail:
            raise RpcError("down")
        headers = self.tip if self.headers is None else self.headers
        return {"chain": self.chain, "blocks": self.tip, "headers": headers,
                "initialblockdownload": self.ibd}

    def getblockhash(self, height):
        if self.fail:
            raise RpcError("down")
        if int(height) == BTC_CHECK_HEIGHT:
            return self.split_hash
        if int(height) not in self.by_height:
            raise RpcError("missing-height")
        return self.by_height[int(height)]

    def getblock(self, block_hash, verbosity=2):
        if self.fail:
            raise RpcError("down")
        block = self.by_hash[block_hash]
        height = block["height"]
        active = self.by_height.get(height) == block_hash
        out = {"hash": block_hash, "height": height,
               "confirmations": (self.tip - height + 1) if active else -1,
               "tx": block["tx"]}
        return out


class MatchTests(unittest.TestCase):
    def test_only_the_coinbase_counts(self):
        block = {"tx": [
            {"vin": [{"coinbase": "aa"}], "vout": [{"scriptPubKey": {"address": OTHER}}]},
            {"vin": [{"txid": "yy"}], "vout": [{"scriptPubKey": {"address": PAY}}]},
        ]}
        self.assertFalse(payout_matches(block, (PAY,)))
        block["tx"][0]["vout"][0]["scriptPubKey"] = {"addresses": [PAY]}
        self.assertTrue(payout_matches(block, (PAY,)))
        self.assertIn(PAY, coinbase_addresses(block))

    def test_cashaddr_prefix_and_case(self):
        raw = "q" + ("p" * 41)
        block = {"tx": [{"vin": [{"coinbase": "aa"}],
                         "vout": [{"scriptPubKey": {"address": "bitcoincash:" + raw.upper()}}]}]}
        self.assertTrue(payout_matches(block, (raw,)))


class EnvTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual(chains_from_env({}), ())

    def test_rejects_secrets_in_the_url_and_private_keys(self):
        secret = "s3cret-password"
        with self.assertLogs("blockwatch", level="ERROR") as logs:
            chains = chains_from_env({
                "BLOCK_WATCH": "1",
                "BTC_NODE_RPC_URL": f"http://user:{secret}@127.0.0.1:8332",
                "BTC_NODE_RPC_USER": "u",
                "BTC_NODE_RPC_PASSWORD": "p",
                "BTC_PAYOUT_ADDRESSES": PAY,
            })
        self.assertEqual(chains, ())
        self.assertNotIn(secret, "\n".join(logs.output))
        key = "5" + ("1" * 50)
        with self.assertLogs("blockwatch", level="ERROR") as logs:
            chains = chains_from_env({
                "BLOCK_WATCH": "1",
                "BTC_NODE_RPC_URL": "http://127.0.0.1:8332",
                "BTC_NODE_RPC_USER": "u",
                "BTC_NODE_RPC_PASSWORD": "p",
                "BTC_PAYOUT_ADDRESSES": key,
            })
        self.assertEqual(chains, ())
        self.assertNotIn(key, "\n".join(logs.output))

    def test_cookie_file_is_accepted_and_not_logged(self):
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "cookie")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("rpcuser:rpc-secret\n")
        chains = chains_from_env({
            "BLOCK_WATCH": "1",
            "BCH_NODE_RPC_URL": "https://bch-node.example/rpc",
            "BCH_NODE_RPC_COOKIE": path,
            "BCH_PAYOUT_ADDRESSES": BCH_PAY,
        })
        self.assertEqual(len(chains), 1)
        self.assertEqual(chains[0].name, "BCH")
        self.assertEqual(chains[0].user, "rpcuser")
        self.assertNotIn("rpc-secret", chains[0].url)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.sqlite3"))
        self.telegram = []
        self.phone = []
        self.node = Node()
        self.node.add(100, None)
        self.node.tip = 100
        chain = ChainSettings("BTC", "http://127.0.0.1:8332", "u", "p", (PAY,), "btc")
        self.watch = BlockWatch(
            self.store, self.telegram.append,
            lambda text, kind, miner: self.phone.append((kind, text)),
            (chain,), rpc_factory=lambda _c: self.node, max_blocks_per_poll=10,
        )

    def test_baseline_match_restart_settle_and_reorg(self):
        self.watch.poll(1_000)
        self.assertEqual(self.telegram, [])
        self.assertEqual(self.store.get("blockwatch:BTC")["cursor"], 100)

        paid = self.node.add(101, PAY)
        self.node.tip = 101
        self.watch.poll(1_060)
        self.assertEqual(len(self.telegram), 1)
        self.assertIn("in the active chain", self.telegram[0])
        self.assertIn("Not final", self.telegram[0])
        self.assertIn("does not identify which miner", self.telegram[0])
        self.assertIn(paid, self.telegram[0])
        self.assertNotIn(PAY, self.telegram[0])
        self.assertNotIn(PAY, self.phone[0][1])
        self.assertEqual(self.phone[0][0], "block_active")

        self.watch.poll(1_120)
        self.assertEqual(len(self.telegram), 1)

        again = BlockWatch(
            self.store, self.telegram.append,
            lambda text, kind, miner: self.phone.append((kind, text)),
            self.watch.chains, rpc_factory=lambda _c: self.node,
        )
        again.poll(1_180)
        self.assertEqual(len(self.telegram), 1)

        for height in range(102, 107):
            self.node.add(height, None)
        self.node.tip = 106
        self.watch.poll(1_300)
        self.assertEqual(len(self.telegram), 2)
        self.assertIn("still in the active chain after 6 confirmations", self.telegram[1])
        self.assertIn("not a promise", self.telegram[1])
        self.assertNotIn(PAY, self.telegram[1])

        for height in range(101, 107):
            self.node.replace(height, PAY if height == 101 else None)
        self.watch.poll(1_360)
        kinds = [text for text in self.telegram]
        self.assertTrue(any("left the active chain" in text for text in kinds))
        self.assertTrue(any(text.startswith("BTC payout block is in the active chain.") for text in kinds[2:]))
        self.watch.poll(1_420)
        self.assertEqual(len([t for t in self.telegram if "left the active chain" in t]), 1)
        for text in self.telegram:
            self.assertNotIn(PAY, text)
        for _kind, text in self.phone:
            self.assertNotIn(PAY, text)

    def test_ignores_other_payouts_and_non_coinbase(self):
        self.watch.poll(1)
        self.node.add(101, OTHER, second=PAY)
        self.node.tip = 101
        self.watch.poll(2)
        self.assertEqual(self.telegram, [])

    def test_first_run_during_sync_does_not_announce_old_blocks(self):
        self.node.ibd = True
        self.node.add(100, PAY)
        self.watch.poll(1)
        self.assertEqual(self.telegram, [])
        self.assertIsNone(self.store.get("blockwatch:BTC").get("cursor"))
        self.node.ibd = False
        self.watch.poll(2)
        self.assertEqual(self.telegram, [])
        self.node.add(101, PAY)
        self.node.tip = 101
        self.watch.poll(3)
        self.assertEqual(len(self.telegram), 1)

    def test_wrong_chain_and_unreachable_node(self):
        self.node.split_hash = "ab" * 32
        self.watch.poll(1)
        self.assertEqual(self.telegram, [])
        self.node.split_hash = BTC_BLOCK_500000
        self.node.fail = True
        self.watch.poll(2)
        self.assertEqual(self.telegram, [])

    def test_bch_label_and_partial_scan(self):
        node = Node(split="cd" * 32)
        node.add(50, None)
        node.tip = 50
        chain = ChainSettings("BCH", "http://127.0.0.1:8332", "u", "p", (BCH_PAY,), "bch")
        watch = BlockWatch(
            self.store, self.telegram.append, lambda text, kind, miner: self.phone.append((kind, text)),
            (chain,), rpc_factory=lambda _c: node, max_blocks_per_poll=1,
        )
        watch.poll(1)
        node.add(51, BCH_PAY.split(":", 1)[1])
        node.add(52, BCH_PAY)
        node.tip = 52
        watch.poll(2)
        self.assertEqual(len(self.telegram), 1)
        self.assertTrue(self.telegram[0].startswith("BCH "))
        watch.poll(3)
        self.assertEqual(len(self.telegram), 2)
        self.assertNotIn(BCH_PAY, "\n".join(self.telegram))

    def test_phone_failure_still_records_one_telegram_alert(self):
        def boom(text, kind, miner):
            raise RuntimeError("phone down")

        self.watch.phone = boom
        self.watch.poll(1)
        self.node.add(101, PAY)
        self.node.tip = 101
        self.watch.poll(2)
        self.assertEqual(len(self.telegram), 1)
        self.watch.poll(3)
        self.assertEqual(len(self.telegram), 1)


class RpcTests(unittest.TestCase):
    def test_error_hides_the_password_and_url(self):
        secret = "rpc-secret-value"

        def opener(req, timeout=None):
            self.assertNotIn(secret, req.full_url)
            self.assertTrue(req.get_header("Authorization").startswith("Basic "))
            raise urllib.error.URLError("timed out " + secret)

        rpc = JsonRpc("http://127.0.0.1:8332", "user", secret, opener=opener)
        with self.assertRaises(RpcError) as caught:
            rpc.getblockchaininfo()
        self.assertEqual(str(caught.exception), "URLError")
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(secret, str(caught.exception))

    def test_result_is_returned(self):
        def opener(req, timeout=None):
            class Resp:
                def read(self):
                    return json.dumps({"result": {"blocks": 3}, "error": None}).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            return Resp()

        rpc = JsonRpc("http://127.0.0.1:8332", "user", "pw", opener=opener)
        self.assertEqual(rpc.getblockchaininfo()["blocks"], 3)


class AppOffTests(unittest.TestCase):
    def test_existing_app_leaves_block_watch_off(self):
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "c.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("network: {enabled: false}\nminers: []\n")
        with patch.dict(os.environ, {"BLOCK_WATCH": "", "TELEGRAM_CHAT_ID": "4242"}, clear=False):
            app = App(load_config(path), folder, dry_run=True)
        self.addCleanup(app.pool.shutdown, wait=False)
        self.addCleanup(app.store.db.close)
        self.assertFalse(app.blocks.enabled)


if __name__ == "__main__":
    unittest.main()
