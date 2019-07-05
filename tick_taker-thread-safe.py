import sys
sys.path.append('lib/alpacahq/alpaca-trade-api-python')
sys.path.append('../alpaca-trade-api-python')
sys.path.append('../..')
import threading
import argparse
import pandas as pd
import numpy as np
from random import randint
import alpaca_trade_api as tradeapi
from StockInvestHawk.utils.Logger import Logger

logger = Logger()
ORDER_LOCK = threading.Lock()


class Quote():
    """
    We use Quote objects to represent the bid/ask spread. When we encounter a
    'level change', a move of exactly 1 penny, we may attempt to make one
    trade. Whether or not the trade is successfully filled, we do not submit
    another trade until we see another level change.

    Note: Only moves of 1 penny are considered eligible because larger moves
    could potentially indicate some newsworthy event for the stock, which this
    algorithm is not tuned to trade.
    """

    def __init__(self, args):
        self.args = args
        self.prev_bid = 0
        self.prev_ask = 0
        self.prev_spread = 0
        self.bid = 0
        self.ask = 0
        self.bid_size = 0
        self.ask_size = 0
        self.spread = 0
        self.traded = True
        self.level_ct = 1
        self.time = 0

    def reset(self):
        # Called when a level change happens
        self.traded = False
        self.level_ct += 1

    def update(self, data):
        # Update bid and ask sizes and timestamp
        self.bid_size = data.bidsize
        self.ask_size = data.asksize
        logger.debug(f'Update: data: {data}')

        # Check if there has been a level change
        if (
            self.bid != data.bidprice
            and self.ask != data.askprice
            and round(data.askprice - data.bidprice, 2) == self.args.delta_price
        ):
            # Update bids and asks and time of level change
            self.prev_bid = self.bid
            self.prev_ask = self.ask
            self.bid = data.bidprice
            self.ask = data.askprice
            self.time = data.timestamp
            # Update spreads
            self.prev_spread = round(self.prev_ask - self.prev_bid, 3)
            self.spread = round(self.ask - self.bid, 3)
            logger.console(
                f'Level change: {self.prev_bid}, {self.prev_ask}, '
                f'{self.prev_spread}, {self.bid}, {self.ask}, {self.spread}, '
                f'{self.bid_size}, {self.ask_size}'
            )
            # If change is from one penny spread level to a different penny
            # spread level, then initialize for new level (reset stale vars)
            if self.prev_spread == self.args.delta_price:
                self.reset()

    def __str__(self):
        return (f'Prev Bid: {self.prev_bid}, Prev Ask:{self.prev_ask}, '
                f'Pref Spread: {self.prev_spread}, Bid: {self.bid}, '
                f'Ask: {self.ask}, Bid Size: {self.bid_size}, '
                f'Ask Size: {self.ask_size}, Spread: {self.spread}, '
                f'Traded: {self.traded}, Level Ct: {self.level_ct}, '
                f'Time: {self.time}')


class Position():
    """
    The position object is used to track how many shares we have. We need to
    keep track of this so our position size doesn't inflate beyond the level
    we're willing to trade with. Because orders may sometimes be partially
    filled, we need to keep track of how many shares are "pending" a buy or
    sell as well as how many have been filled into our account.
    """

    def __init__(self, args):
        self.args = args
        self.orders_filled_amount = {}
        self.pending_buy_shares = 0
        self.pending_sell_shares = 0
        self.total_shares = 0

    def update_order_ammount(self, order, ammount):
        self.orders_filled_amount[order] = ammount

    def get_order_ammount(self, order):
        with ORDER_LOCK:
            return self.orders_filled_amount.get(order)

    def del_order(self, order):
        with ORDER_LOCK:
            if self.orders_filled_amount.get(order):
                del self.orders_filled_amount[order]

    def update_pending_buy_shares(self, quantity):
        with ORDER_LOCK:
            self.pending_buy_shares += quantity

    def update_pending_sell_shares(self, quantity):
        with ORDER_LOCK:
            self.pending_sell_shares += quantity

    def update_filled_amount(self, order_id, new_amount, side):
        old_amount = self.get_order_ammount(order_id)
        if old_amount:
            if new_amount > old_amount:
                if side == 'buy':
                    self.update_pending_buy_shares(old_amount - new_amount)
                    self.update_total_shares(new_amount - old_amount)
                else:
                    self.update_pending_sell_shares(old_amount - new_amount)
                    self.update_total_shares(old_amount - new_amount)
                self.update_order_ammount(order_id, new_amount)
        else:
            logger.console(
                f'Order ID: {order_id} not present on current orders.')

    def remove_pending_order(self, order_id, side):
        old_amount = self.get_order_ammount(order_id)
        if old_amount:
            if side == 'buy':
                self.update_pending_buy_shares(
                    old_amount - self.args.quantity)
            else:
                self.update_pending_sell_shares(
                    old_amount - self.args.quantity)
            #del self.orders_filled_amount[order_id]
            self.del_order(order_id)
        else:
            logger.console(
                f'Order ID: {order_id} not present on current orders.')

    def update_total_shares(self, quantity):
        with ORDER_LOCK:
            self.total_shares += quantity


def run(args):
    symbol = args.symbol
    max_shares = args.max_shares
    opts = {}
    if args.key_id:
        opts['key_id'] = args.key_id
    if args.secret_key:
        opts['secret_key'] = args.secret_key
    if args.base_url:
        opts['base_url'] = args.base_url
    elif 'key_id' in opts and opts['key_id'].startswith('PK'):
        opts['base_url'] = 'https://paper-api.alpaca.markets'
    # Create an API object which can be used to submit orders, etc.
    api = tradeapi.REST(**opts)

    symbol = symbol.upper()
    quote = Quote(args)
    qc = 'Q.%s' % symbol
    tc = 'T.%s' % symbol
    position = Position(args)

    # Establish streaming connection
    conn = tradeapi.StreamConn(**opts)

    # Define our message handling
    @conn.on(r'Q\.' + symbol)
    async def on_quote(conn, channel, data):
        # Quote update received
        logger.trace(
            f'on_quote: conn: {conn}, channel: {channel}, data: {data}')
        quote.update(data)

    @conn.on(r'T\.' + symbol)
    async def on_trade(conn, channel, data):
        logger.trace(
            f'on_trade: conn: {conn}, channel: {channel}, data: {data}')
        if quote.traded:
            return
        # We've received a trade and might be ready to follow it
#         if (
#             data.timestamp <= (
#                 quote.time + pd.Timedelta(np.timedelta64(50, 'ms'))
#             )
#         ):
#             # The trade came too close to the quote update
#             # and may have been for the previous level
#             return
        if data.size >= args.quantity:
            # The trade was large enough to follow, so we check to see if
            # we're ready to trade. We also check to see that the
            # bid vs ask quantities (order book imbalance) indicate
            # a movement in that direction. We also want to be sure that
            # we're not buying or selling more than we should.
            logger.console(
                f'Analyze buy/sell...\n\tData: {data}\n\tQuote: {quote}')
            if (
                data.price == quote.ask
                and quote.bid_size > (quote.ask_size * 1.8)
                and (
                    position.total_shares + position.pending_buy_shares
                ) < args.max_shares - args.quantity
            ):
                # Everything looks right, so we submit our buy at the ask
                try:
                    logger.trace('Buying...')
                    id = f'{randint(1,99999999):08}-{randint(1,9999):04}-{randint(1,9999):04}-{randint(1,9999):04}-{randint(1,999999999999):012}'
                    logger.trace(f'Updating order ammount of {id} to 0.')
                    with ORDER_LOCK:
                        position.update_order_ammount(id, 0)
                        o = api.submit_order(
                            client_order_id=id, symbol=symbol, qty=args.quantity, side='buy',
                            type='limit', time_in_force='day',
                            limit_price=str(quote.ask)
                        )
                    # Approximate an IOC order by immediately cancelling
                    api.cancel_order(o.id)
                    position.update_pending_buy_shares(args.quantity)
                    logger.console(
                        f'ID: {o.client_order_id}, Buy at {quote.ask}')
                    quote.traded = True
                except Exception as e:
                    logger.trace(e)
                    logger.console(e)
            elif (
                data.price == quote.bid
                and quote.ask_size > (quote.bid_size * 1.8)
                and (
                    position.total_shares - position.pending_sell_shares
                ) >= args.quantity
            ):
                # Everything looks right, so we submit our sell at the bid
                try:
                    logger.trace('Selling...')
                    id = f'{randint(1,99999999):08}-{randint(1,9999):04}-{randint(1,9999):04}-{randint(1,9999):04}-{randint(1,999999999999):012}'
                    with ORDER_LOCK:
                        position.update_order_ammount(id, 0)
                        o = api.submit_order(
                            client_order_id=id, symbol=symbol, qty=args.quantity, side='sell',
                            type='limit', time_in_force='day',
                            limit_price=str(float(quote.bid) + 0.01)
                        )
                        logger.trace(
                            f'Updating order ammount of {o.client_order_id} to 0.')
                    # Approximate an IOC order by immediately cancelling
                    api.cancel_order(o.id)
                    position.update_pending_sell_shares(args.quantity)
                    logger.console(
                        f'ID: {o.client_order_id}, Sell at {quote.bid}')
                    quote.traded = True
                except Exception as e:
                    logger.trace(e)
                    logger.console(e)

    @conn.on(r'trade_updates')
    async def on_trade_updates(conn, channel, data):
        # We got an update on one of the orders we submitted. We need to
        # update our position with the new information.
        with ORDER_LOCK:
            pass
        logger.trace(
            f'on_trade_updates: conn: {conn}, channel: {channel}, data: {data}')
        event = data.event
        if event == 'fill':
            if data.order['side'] == 'buy':
                position.update_total_shares(
                    int(data.order['filled_qty'])
                )
                try:
                    id = f'{randint(1,99999999):08}-{randint(1,9999):04}-{randint(1,9999):04}-{randint(1,9999):04}-{randint(1,999999999999):012}'
                    with ORDER_LOCK:
                        position.update_order_ammount(
                            data.order['client_order_id'], 0)
                        o = api.submit_order(
                            client_order_id=id, symbol=data.order['symbol'], qty=data.order['filled_qty'], side='sell',
                            type='limit', time_in_force='day',
                            limit_price=str(float(data.price) + 0.01)
                        )
                    logger.trace(
                        f'Updating order ammount of {o.client_order_id} to 0.')
                    position.update_pending_sell_shares(args.quantity)
                except Exception as e:
                    logger.trace(e)
                    logger.console(e)
            else:
                position.update_total_shares(
                    -1 * int(data.order['filled_qty'])
                )
            position.remove_pending_order(
                data.order['client_order_id'], data.order['side']
            )
        elif event == 'partial_fill':
            position.update_filled_amount(
                data.order['client_order_id'], int(data.order['filled_qty']),
                data.order['side']
            )
        elif event == 'canceled' or event == 'rejected':
            position.remove_pending_order(
                data.order['client_order_id'], data.order['side']
            )

    conn.run(
        ['trade_updates', tc, qc]
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--symbol', type=str, default='SNAP',
        help='Symbol you want to trade.'
    )
    parser.add_argument(
        '--quantity', type=int, default=100,
        help='Maximum number of shares to hold at once. Minimum 1.'
    )
    parser.add_argument(
        '--max-shares', type=int, default=100,
        help='Maximum number of shares to hold at once. Maximum 100.'
    )
    parser.add_argument(
        '--delta-price', type=float, default=.01,
        help='Delta between ask/bid price to consider purchase.'
    )
    parser.add_argument(
        '--key-id', type=str, default=None,
        help='API key ID',
    )
    parser.add_argument(
        '--secret-key', type=str, default=None,
        help='API secret key',
    )
    parser.add_argument(
        '--base-url', type=str, default=None,
        help='set https://paper-api.alpaca.markets if paper trading',
    )
    args = parser.parse_args()
    assert args.quantity >= 1
    run(args)
