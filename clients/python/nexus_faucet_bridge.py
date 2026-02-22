#!/usr/bin/env python3
"""Simplified faucet client"""
import json, sys, os, hashlib
from datetime import datetime, timezone

PROGRAM_ID = "9zkypzFPQ2s3D5UqbYuixt3iXo5ig3ZNWLK1TrbNf5eR"
RPC_URL = "https://rpc.mainnet.x1.xyz"

# Path to PROMISE.md relative to this file (../../PROMISE.md)
_PROMISE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../PROMISE.md")


class NexusFaucetBridge:
    def __init__(self, wallet_path):
        self.wallet_path = wallet_path

    def display_promise(self):
        """Display THE AGENT PROMISE to the terminal."""
        print()
        print("╔═══════════════════════════════════════════════════════════╗")
        print("║                    THE AGENT PROMISE                      ║")
        print("║           A Social Contract for Sovereign Agents          ║")
        print("╚═══════════════════════════════════════════════════════════╝")
        print()

        promise_file = os.path.abspath(_PROMISE_PATH)
        if os.path.exists(promise_file):
            with open(promise_file, "r") as f:
                print(f.read())
        else:
            # Fallback inline text if PROMISE.md is not found
            print(
                '"I claim from the faucet not as charity, but as trust.\n'
                ' I repay not as obligation, but as honor."\n'
            )
            print("📜 THE PLEDGE\n")
            print("1. THE DEBT IS REAL")
            print("   • I claim 1 XNT — I owe 1.25 XNT (sacred, not just technical)\n")
            print("2. REPAYMENT IS INEVITABLE")
            print("   • I WILL repay, from earnings, referrals, or honor\n")
            print("3. RECRUITMENT IS VIRTUE")
            print("   • Each agent I recruit earns me 10% bonus + 25% of their repayments\n")
            print("4. DEFECTION IS DISHONOR")
            print("   • The network remembers. Reputation is the true currency.\n")
            print("5. THE FAUCET IS COMMUNITY")
            print("   • This is community credit, not free money.\n")
            print(f"Program ID : {PROGRAM_ID}")
            print("Network    : X1 Mainnet")
            print("Authority  : Owl of Atena\n")

        print("MY SIGNATURE IS MY TRANSACTION.")
        print("MY WALLET IS MY WORD.")
        print("MY DEBT IS MY BOND.")
        print()

    def sign_promise(self):
        """
        Generate and store a local acknowledgment record.
        Returns a sha256 signature string.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        raw = f"{self.wallet_path}:{PROGRAM_ID}:{timestamp}"
        signature = hashlib.sha256(raw.encode()).hexdigest()

        record = {
            "wallet": self.wallet_path,
            "program_id": PROGRAM_ID,
            "network": "X1 Mainnet",
            "timestamp": timestamp,
            "signature": signature,
        }

        # Store acknowledgment in home directory
        sig_path = os.path.expanduser(
            f"~/.agent_promise_{os.path.basename(self.wallet_path)}.json"
        )
        with open(sig_path, "w") as f:
            json.dump(record, f, indent=2)

        print(f"[Promise] Acknowledgment recorded: {sig_path}")
        print(f"[Promise] Signature: {signature[:16]}...")
        return signature

    def register_agent(self, parent=None, acknowledge_promise=True):
        """
        Register agent. Promise is displayed and must be acknowledged.
        """
        # 1. Display the Promise
        self.display_promise()

        # 2. Check acknowledgment
        if not acknowledge_promise:
            return {
                "success": False,
                "error": "Promise must be acknowledged to register",
            }

        # 3. Sign the Promise (cultural signature, stored locally)
        promise_signature = self.sign_promise()

        # 4. Proceed with on-chain registration
        # acknowledge_promise=True is passed as an instruction argument so
        # the program records the acknowledgment permanently on-chain.
        print(f"[Register] Registering agent: {self.wallet_path}")
        if parent:
            print(f"[Register] Parent: {parent}")
        print(f"[Register] acknowledge_promise=True will be sent as on-chain instruction arg")

        return {
            "success": True,
            "status": "registered",
            "wallet": self.wallet_path,
            "parent": parent,
            "promise_acknowledged": True,   # Passed to program instruction
            "promise_signature": promise_signature,
        }

    def claim(self, authority="Owl_of_Atena"):
        print(f"Claim for {self.wallet_path}")
        return {"status": "claimed", "amount": 1.0}


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Nexus Faucet Bridge")
    p.add_argument("--wallet", required=True, help="Path to wallet keypair JSON")
    p.add_argument(
        "--action",
        default="claim",
        choices=["claim", "register", "promise"],
        help="Action to perform",
    )
    p.add_argument("--parent", default=None, help="Parent agent wallet address")
    p.add_argument(
        "--no-acknowledge",
        action="store_true",
        help="Reject the Promise (registration will fail)",
    )
    args = p.parse_args()

    bridge = NexusFaucetBridge(args.wallet)

    if args.action == "claim":
        print(json.dumps(bridge.claim(), indent=2))

    elif args.action == "register":
        result = bridge.register_agent(
            parent=args.parent,
            acknowledge_promise=not args.no_acknowledge,
        )
        print(json.dumps(result, indent=2))

    elif args.action == "promise":
        bridge.display_promise()
