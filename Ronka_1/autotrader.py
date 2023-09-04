# Copyright 2021 Optiver Asia Pacific Pty. Ltd.
#
# This file is part of Ready Trader Go.
#
#     Ready Trader Go is free software: you can redistribute it and/or
#     modify it under the terms of the GNU Affero General Public License
#     as published by the Free Software Foundation, either version 3 of
#     the License, or (at your option) any later version.
#
#     Ready Trader Go is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU Affero General Public License for more details.
#
#     You should have received a copy of the GNU Affero General Public
#     License along with Ready Trader Go.  If not, see
#     <https://www.gnu.org/licenses/>.
import asyncio
import itertools
import time


from typing import List
from ready_trader_go import BaseAutoTrader, Instrument, Lifespan, MAXIMUM_ASK, MINIMUM_BID, Side


LOT_SIZE = 10
POSITION_LIMIT = 100
TICK_SIZE_IN_CENTS = 100
MIN_BID_NEAREST_TICK = (
    MINIMUM_BID + TICK_SIZE_IN_CENTS) // TICK_SIZE_IN_CENTS * TICK_SIZE_IN_CENTS
MAX_ASK_NEAREST_TICK = MAXIMUM_ASK // TICK_SIZE_IN_CENTS * TICK_SIZE_IN_CENTS


class RateLimiter:
    def __init__(self, limit: int, interval: float):
        self.limit = limit
        self.interval = interval
        self.requests = []

    def get_wait_time(self):
        now = time.time()
        self.requests = [r for r in self.requests if r >= now - self.interval]
        if len(self.requests) >= self.limit:
            return self.requests[0] + self.interval - now
        else:
            return 0

    def wait(self):
        wait_time = self.get_wait_time()
        if wait_time > 0:
            time.sleep(wait_time)
        self.requests.append(time.time())


class AutoTrader(BaseAutoTrader):
    def __init__(self, loop: asyncio.AbstractEventLoop, team_name: str, secret: str):
        """Initialise a new instance of the AutoTrader class."""
        super().__init__(loop, team_name, secret)
        self.order_ids = itertools.count(1)
        self.bids = set()
        self.asks = set()
        self.ask_id = self.ask_price = self.bid_id = self.bid_price = self.position = 0
        self.rate_limiter = RateLimiter(50, 1)
        self.ask_volumes = [0] * 5
        self.bid_volumes = [0] * 5

    def send_insert_order(self, client_order_id: int, side: Side, price: int, volume: int, lifespan: Lifespan) -> None:
        """Send an insert order request to the exchange."""
        self.rate_limiter.wait()  # Wait for rate limit
        super().send_insert_order(client_order_id, side, price, volume, lifespan)

    def send_cancel_order(self, order_id: int) -> None:
        """Send a cancel order request to the exchange."""
        self.rate_limiter.wait()  # Wait for rate limit
        super().send_cancel_order(order_id)

    def send_amend_order(self, order_id: int, price: int, volume: int) -> None:
        """Send an amend order request to the exchange."""
        self.rate_limiter.wait()  # Wait for rate limit
        super().send_amend_order(order_id, price, volume)

    def on_error_message(self, client_order_id: int, error_message: bytes) -> None:
        """Called when the exchange detects an error.
        If the error pertains to a particular order, then the client_order_id
        will identify that order, otherwise the client_order_id will be zero.
        """
        self.logger.warning("error with order %d: %s",
                            client_order_id, error_message.decode())
        if client_order_id != 0 and (client_order_id in self.bids or client_order_id in self.asks):
            self.on_order_status_message(client_order_id, 0, 0, 0)

    def on_hedge_filled_message(self, client_order_id: int, price: int, volume: int) -> None:
        """Called when one of your hedge orders is filled.
        The price is the average price at which the order was (partially) filled,
        which may be better than the order's limit price. The volume is
        the number of lots filled at that price.
        """
        self.logger.info("received hedge filled for order %d with average price %d and volume %d", client_order_id,
                         price, volume)

    def on_order_filled_message(self, client_order_id: int, price: int, volume: int) -> None:
        """Called when one of your orders is filled, partially or fully.
        The price is the price at which the order was (partially) filled,
        which may be better than the order's limit price. The volume is
        the number of lots filled at that price.
        """
        self.logger.info("received order filled for order %d with price %d and volume %d", client_order_id,
                         price, volume)
        if client_order_id in self.bids:
            self.position += volume
            self.send_hedge_order(next(self.order_ids),
                                  Side.ASK, MIN_BID_NEAREST_TICK, volume)
        elif client_order_id in self.asks:
            self.position -= volume
            self.send_hedge_order(next(self.order_ids),
                                  Side.BID, MAX_ASK_NEAREST_TICK, volume)

    def on_order_status_message(self, client_order_id: int, fill_volume: int, remaining_volume: int,
                                fees: int) -> None:
        """Called when the status of one of your orders changes.
        The fill_volume is the number of lots already traded, remaining_volume
        is the number of lots yet to be traded and fees is the total fees for
        this order. Remember that you pay fees for being a market taker, but
        you receive fees for being a market maker, so fees can be negative.
        If an order is cancelled its remaining volume will be zero.
        """
        self.logger.info("received order status for order %d with fill volume %d remaining %d and fees %d",
                         client_order_id, fill_volume, remaining_volume, fees)
        if remaining_volume == 0:
            if client_order_id == self.bid_id:
                self.bid_id = 0
            elif client_order_id == self.ask_id:
                self.ask_id = 0

            # It could be either a bid or an ask
            self.bids.discard(client_order_id)
            self.asks.discard(client_order_id)

    def on_trade_ticks_message(self, instrument: int, sequence_number: int, ask_prices: List[int],
                               ask_volumes: List[int], bid_prices: List[int], bid_volumes: List[int]) -> None:
        """Called periodically when there is trading activity on the market.
        The five best ask (i.e. sell) and bid (i.e. buy) prices at which there
        has been trading activity are reported along with the aggregated volume
        traded at each of those price levels.
        If there are less than five prices on a side, then zeros will appear at
        the end of both the prices and volumes arrays.
        """
        self.logger.info("received trade ticks for instrument %d with sequence number %d", instrument,
                         sequence_number)

    def on_order_book_update_message(self, instrument: int, sequence_number: int, ask_prices: List[int],
                                     ask_volumes: List[int], bid_prices: List[int], bid_volumes: List[int]) -> None:
        self.logger.info(
            "received order book for instrument %d with sequence number %d", instrument, sequence_number)
        if instrument == Instrument.FUTURE:
            # Calculate the midpoint price
            midpoint_price = (bid_prices[0] + ask_prices[0]) // 2

            # Determine the desired buy and sell prices based on the midpoint
            buy_price = midpoint_price - TICK_SIZE_IN_CENTS
            sell_price = midpoint_price + TICK_SIZE_IN_CENTS

            # Get the total available volume on both sides of the market
            total_volume = sum(ask_volumes) + sum(bid_volumes)

                        # Calculate the ratio of the available volume at the best bid and ask prices to the total available volume
            bid_ratio = bid_volumes[0] / \
                total_volume if total_volume > 0 else 0
            ask_ratio = ask_volumes[0] / \
                total_volume if total_volume > 0 else 0

            # Adjust the order size based on the available volume at each price level
            if bid_ratio > 0:
                bid_size = int(LOT_SIZE * min(1 / bid_ratio, 1)
                               if bid_ratio > 0.2 else LOT_SIZE * 0.2)
            else:
                bid_size = int(LOT_SIZE * 0.2)

            
            if ask_ratio > 0:
                ask_size = int(LOT_SIZE * min(1 / ask_ratio, 1)
                               if ask_ratio > 0.2 else LOT_SIZE * 0.2)
            else:
                ask_size = int(LOT_SIZE * 0.2)

            # Cancel any existing orders that are not at the desired prices
            if self.bid_id != 0 and self.bid_price != buy_price:
                self.send_cancel_order(self.bid_id)
                self.bids.discard(self.bid_id)
                self.bid_id = 0
            if self.ask_id != 0 and self.ask_price != sell_price:
                self.send_cancel_order(self.ask_id)
                self.asks.discard(self.ask_id)
                self.ask_id = 0

                        # Place new orders at the desired prices with adjusted size
            if self.bid_id == 0 and self.position < POSITION_LIMIT:
                

                # Limit buy order
                bid_id = next(self.order_ids)
                bid_price = buy_price - TICK_SIZE_IN_CENTS  # Adjust price for limit order
                if self.position + bid_size <= POSITION_LIMIT and bid_price > 0:  # Check if new order would exceed POSITION_LIMIT

                    self.send_insert_order(
                        bid_id, Side.BUY, bid_price, bid_size, Lifespan.GOOD_FOR_DAY)
                    

                    self.bids.add(bid_id)
                    self.bid_id = bid_id
                    self.bid_price = bid_price
            
            if self.ask_id == 0 and self.position > -POSITION_LIMIT:
                # Limit sell order
                ask_id = next(self.order_ids)
                ask_price = sell_price + TICK_SIZE_IN_CENTS  # Adjust price for limit order
                if self.position - ask_size >= -POSITION_LIMIT and ask_price > 0:  # Check if new order would exceed POSITION_LIMIT
                    self.send_insert_order(
                        ask_id, Side.SELL, ask_price, ask_size, Lifespan.GOOD_FOR_DAY)
                    self.asks.add(ask_id)
                    self.ask_id = ask_id
                    self.ask_price = ask_price

                       
