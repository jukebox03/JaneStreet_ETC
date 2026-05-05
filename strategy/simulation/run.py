"""Jane Street ETC local simulator.

Usage:
    python run.py --mode prod --port 25000
    python run.py --mode slower --port 25001
    python run.py --mode empty --port 25002

Then connect your existing bot with --specific-address localhost:PORT.
"""
import argparse
import sys

sys.stdout.reconfigure(line_buffering=True)

from exchange.server import Server


def main():
    parser = argparse.ArgumentParser(
        description="Jane Street ETC local exchange simulator")
    parser.add_argument("--port", type=int, default=25000,
                        help="TCP port to listen on (default: 25000)")
    parser.add_argument("--host", type=str, default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--mode", type=str, default="prod",
                        choices=["prod", "slower", "empty"],
                        help="Marketplace activity level")
    parser.add_argument("--round", type=float, default=300.0,
                        help="Round duration in seconds (default: 300)")
    parser.add_argument("--rounds", type=int, default=None,
                        help="Max number of rounds before shutting down "
                             "(default: unlimited)")
    parser.add_argument("--print-interval", type=float, default=None,
                        metavar="SECONDS",
                        help="Print fair market prices every N seconds "
                             "(default: off)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducibility")
    parser.add_argument("--verbose", action="store_true",
                        help="Print market scenario events")
    args = parser.parse_args()

    activity_map = {"prod": 1.0, "slower": 0.4, "empty": 0.0}
    activity = activity_map[args.mode]

    server = Server(
        host=args.host,
        port=args.port,
        activity=activity,
        round_duration=args.round,
        max_rounds=args.rounds,
        print_interval=args.print_interval,
        seed=args.seed,
        verbose=args.verbose,
    )
    try:
        server.start()
    except KeyboardInterrupt:
        print("\n[server] shutting down")
        server.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()
