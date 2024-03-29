# @date 2018-08-24
# @author Frederic SCHERMA
# @license Copyright (c) 2018 Dream Overflow
# Strategy trader base class.

import threading
import time

from strategy.strategytrade import StrategyTrade
from strategy.strategyassettrade import StrategyAssetTrade
from strategy.strategymargintrade import StrategyMarginTrade
from strategy.strategyindmargintrade import StrategyIndMarginTrade

from terminal.terminal import Terminal
from common.utils import timeframe_to_str
from notifier.signal import Signal

from trader.order import Order
from database.database import Database

import logging
logger = logging.getLogger('siis.strategy')


class StrategyTrader(object):
    """
    A strategy can manage multiple instrument. Strategy trader is on of the managed instruments.

    @todo _global_streamer must be improved. streaming functionnalities must be only connected to the
        notification receiver (only then keep notify_order calls), streaming will be done on a distinct service.
        disable any others streaming capacities on the strategy-traders excepted for debug purposes.
    """

    def __init__(self, strategy, instrument):
        self.strategy = strategy
        self.instrument = instrument

        self.trades = []
        self.regions = []

        self._next_trade_id = 1
        self._next_region_id = 1

        self._mutex = threading.RLock()
        self._activity = True

        self._global_streamer = None
        self._timeframe_streamers = {}

        self._stats = {
            'perf': 0.0,     # initial
            'worst': 0.0,    # worst trade lost
            'best': 0.0,     # best trade profit
            'failed': [],    # failed terminated trades
            'success': [],   # success terminated trades
            'roe': [],       # return to equity trades
            'cont-win': 0,   # contigous win trades
            'cont-loss': 0,  # contigous loss trades
        }

    def lock(self, blocking=True, timeout=-1):
        self._mutex.acquire(blocking, timeout)

    def unlock(self):
        self._mutex.release()

    #
    # processing
    #

    @property
    def activity(self):
        """
        Strategy trader Local state.
        """
        return self._activity

    def set_activity(self, status):
        """
        Enable/disable execution of the automated orders.
        """
        self._activity = status   

    def process(self, timeframe, timestamp):
        """
        Override this method to do her all the strategy work. You must call the update_trades method
        during the process.

        @param timeframe Update timeframe unit.
        @param timestamp Current timestamp (or past time in backtest).
        """
        pass

    #
    # persistance
    #

    def save(self):
        """
        Trader and trades persistance (might occurs only for live mode on real accounts).
        """
        self.lock()

        trader = self.strategy.trader()

        for trade in self.trades:
            t_data = trade.dumps()
            ops_data = [operation.dumps() for operation in trade.operations]

            # store per trade    
            Database.inst().store_user_trade((trader.name, trader.account.name, self.instrument.market_id,
                    self.strategy.identifier, trade.id, trade.trade_type, t_data, ops_data))

        # dumps of regions
        trader_data = {}
        regions_data = [region.dumps() for region in self.regions]

        Database.inst().store_user_trader((trader.name, trader.account.name, self.instrument.market_id,
                self.strategy.identifier, self.activity, trader_data, regions_data))

        self.unlock()

    def loads(self, data, regions):
        """
        Load strategy trader state and regions.
        """
        # data reserved

        # instanciates the regions
        for r in regions:
            if r['name'] in self.strategy.service.regions:
                try:
                    # instanciate the region
                    region = self.strategy.service.regions[r['name']](0, 0, 0, 0)
                    region.loads(r)

                    if region.check():
                        # append the region to the strategy trader
                        strategy_trader.add_region(region)
                    else:
                        logger.error("During loads, region checking error %s" % (r['name'],))

                    self.add_region(region)
                except Exception as e:
                    logger.error(repr(e))
            else:
                logger.error("During loads, unsupported region %s" % (r['name'],))

    def loads_trade(self, trade_id, trade_type, data, operations):
        """
        Load a strategy trader trade and its operations.
        @todo Need to check the validity of the trade :
            - existings orders, create, sell, limit, stop, position
            - and eventually the free margin, asset quantity
        There is many scenarii where the trade state changed, trade executed, order modified or canceled...
        """
        trade = None

        if trade_type == StrategyTrade.TRADE_BUY_SELL:
            trade = StrategyAssetTrade(0)
        elif trade_type == StrategyTrade.TRADE_MARGIN:
            trade = StrategyMarginTrade(0)
        elif trade_type == StrategyTrade.TRADE_IND_MARGIN:
            trade = StrategyIndMarginTrade(0)
        else:
            logger.error("During loads, usupported trade type %i" % (trade_type,))
            return

        trade.loads(data, self.strategy.service)

        # operations
        for op in operations:
            if op['name'] in self.strategy.service.tradeops:
                try:                
                    operation = self.strategy.service.tradeops[op['name']]()
                    operation.loads(op)

                    if operation.check(trade):
                        # append the operation to the trade
                        trade.add_operation(operation)
                    else:
                        logger.error("During loads, operation checking error %s" % (op_name,))
                except Exception as e:
                    logger.error(repr(e))
            else:
                logger.error("During loads, region checking error %s" % (r['name'],))

        self.add_trade(trade)

    #
    # order/position slot
    #

    def order_signal(self, signal_type, data):
        """
        Update quantity/filled on a trade, deleted or canceled.
        """
        self.lock()

        try:
            for trade in self.trades:
                # update each trade relating the order (might be a unique)
                order_id = data[1]['id'] if type(data[1]) is dict else data[1]
                ref_order_id = data[2] if (len(data) > 2 and type(data[2]) is str) else None

                if trade.is_target_order(order_id, ref_order_id):
                    trade.order_signal(signal_type, data[1], data[2] if len(data) > 2 else None, self.instrument)

        except Exception as e:
            logger.error(repr(e))

        self.unlock()

    def position_signal(self, signal_type, data):
        """
        Update quantity/filled on a trade, delete or cancel.
        """
        self.lock()

        try:
            for trade in self.trades:
                # update each trade relating the position (could be many)
                position_id = data[1]['id'] if type(data[1]) is dict else data[1]
                ref_order_id = data[2] if (len(data) > 2 and type(data[2]) is str) else None

                if trade.is_target_position(position_id, ref_order_id):
                    trade.position_signal(signal_type, data[1], data[2] if len(data) > 2 else None, self.instrument)

        except Exception as e:
            logger.error(repr(e))

        self.unlock()

    #
    # trade
    #

    def add_trade(self, trade):
        """
        Add a new trade.
        """
        if not trade:
            return False

        self.lock()

        trade.id = self._next_trade_id
        self._next_trade_id += 1

        self.trades.append(trade)
        self.unlock()

    def remove_trade(self, trade):
        """
        Remove an existing trade.
        """
        if not trade:
            return False

        self.lock()
        self.trades.remove(trade)
        self.unlock()

    def update_trades(self, timestamp):
        """
        Update managed trades per instruments and delete terminated trades.
        """
        if not self.trades:
            return

        trader = self.strategy.trader()

        #
        # for each trade check if the TP or SL is reached and trigger if necessary
        #

        self.lock()

        for trade in self.trades:

            #
            # managed operation
            #

            if trade.has_operations():
                mutated = False

                for operation in trade.operations:
                    mutated |= operation.test_and_operate(trade, self.instrument, trader)

                if mutated:
                    trade.cleanup_operations()

            #
            # active trade
            #

            if trade.is_active():
                # for statistics usage
                trade.update_stats(self.instrument.close_exec_price(trade.direction), timestamp)

            #
            # asset trade
            #

            if trade.trade_type == StrategyTrade.TRADE_BUY_SELL:
                if trade.is_closed():
                    continue

                # process only on active trades
                if not trade.is_active():
                    # @todo timeout if not filled before condition...
                    continue

                if trade.is_closing():
                    continue

                if not self.instrument.tradeable:
                    continue

                # potential order exec close price (always close a long)
                close_exec_price = self.instrument.close_exec_price(Order.LONG)

                if (trade.tp > 0) and (close_exec_price >= trade.tp) and not trade.has_limit_order():
                    # take profit trigger stop, close at market (taker fee)
                    if trade.close(trader, self.instrument.market_id):
                        # estimed profit/loss rate
                        profit_loss_rate = (close_exec_price - trade.entry_price) / trade.entry_price

                        # estimed maker/taker fee rate for entry and exit
                        if trade.get_stats()['entry-maker']:
                            profit_loss_rate -= self.instrument.maker_fee
                        else:
                            profit_loss_rate -= self.instrument.taker_fee

                        if trade.get_stats()['exit-maker']:
                            profit_loss_rate -= self.instrument.maker_fee
                        else:
                            profit_loss_rate -= self.instrument.taker_fee

                        # notify
                        self.strategy.notify_order(trade.id, Order.SHORT, self.instrument.market_id,
                                self.instrument.format_price(close_exec_price), timestamp, trade.timeframe,
                                'take-profit', profit_loss_rate)

                        # streaming (but must be done with notify)
                        if self._global_streamer:
                            self._global_streamer.member('buy-exit').update(close_exec_price, timestamp)

                elif (trade.sl > 0) and (close_exec_price <= trade.sl) and not trade.has_stop_order():
                    # stop loss trigger stop, close at market (taker fee)
                    if trade.close(trader, self.instrument.market_id):
                        # estimed profit/loss rate
                        profit_loss_rate = (close_exec_price - trade.entry_price) / trade.entry_price

                        # estimed maker/taker fee rate for entry and exit
                        if trade.get_stats()['entry-maker']:
                            profit_loss_rate -= self.instrument.maker_fee
                        else:
                            profit_loss_rate -= self.instrument.taker_fee

                        if trade.get_stats()['exit-maker']:
                            profit_loss_rate -= self.instrument.maker_fee
                        else:
                            profit_loss_rate -= self.instrument.taker_fee

                        # notify
                        self.strategy.notify_order(trade.id, Order.SHORT, self.instrument.market_id,
                                self.instrument.format_price(close_exec_price), timestamp, trade.timeframe,
                                'stop-loss', profit_loss_rate)

                        # streaming (but must be done with notify)
                        if self._global_streamer:
                            self._global_streamer.member('buy-exit').update(close_exec_price, timestamp)

            #
            # margin trade
            #

            elif trade.trade_type == StrategyTrade.TRADE_MARGIN or trade.trade_type == StrategyTrade.TRADE_IND_MARGIN:
                # process only on active trades
                if not trade.is_active():
                    # @todo timeout if not filled before condition...
                    continue

                if trade.is_closed():
                    continue

                if trade.is_closing():
                    continue

                if not self.instrument.tradeable:
                    continue

                # potential order exec close price
                close_exec_price = self.instrument.close_exec_price(trade.direction)

                if (trade.tp > 0) and ((trade.direction > 0 and close_exec_price >= trade.tp) or (trade.direction < 0 and close_exec_price <= trade.tp)) and not trade.has_limit_order():
                    # close in profit at market (taker fee)
                    if trade.close(trader, self.instrument.market_id):
                        # estimed profit/loss rate
                        if trade.direction > 0 and trade.entry_price:
                            profit_loss_rate = (close_exec_price - trade.entry_price) / trade.entry_price
                        elif trade.direction < 0 and trade.entry_price:
                            profit_loss_rate = (trade.entry_price - close_exec_price) / trade.entry_price
                        else:
                            profit_loss_rate = 0

                        # estimed maker/taker fee rate for entry and exit
                        if trade.get_stats()['entry-maker']:
                            profit_loss_rate -= self.instrument.maker_fee
                        else:
                            profit_loss_rate -= self.instrument.taker_fee

                        if trade.get_stats()['exit-maker']:
                            profit_loss_rate -= self.instrument.maker_fee
                        else:
                            profit_loss_rate -= self.instrument.taker_fee

                        # and notify
                        self.strategy.notify_order(trade.id, trade.close_direction(), self.instrument.market_id,
                                self.instrument.format_price(close_exec_price), timestamp, trade.timeframe,
                                'take-profit', profit_loss_rate)

                        # and for streaming
                        if self._global_streamer:
                            self._global_streamer.member('sell-exit' if trade.direction < 0 else 'buy-exit').update(close_exec_price, timestamp)

                elif (trade.sl > 0) and ((trade.direction > 0 and close_exec_price <= trade.sl) or (trade.direction < 0 and close_exec_price >= trade.sl)) and not trade.has_stop_order():
                    # close a long or a short position at stop-loss level at market (taker fee)
                    if trade.close(trader, self.instrument.market_id):
                        # estimed profit/loss rate
                        if trade.direction > 0 and trade.entry_price:
                            profit_loss_rate = (close_exec_price - trade.entry_price) / trade.entry_price
                        elif trade.direction < 0 and trade.entry_price:
                            profit_loss_rate = (trade.entry_price - close_exec_price) / trade.entry_price
                        else:
                            profit_loss_rate = 0

                        # estimed maker/taker fee rate for entry and exit
                        if trade.get_stats()['entry-maker']:
                            profit_loss_rate -= self.instrument.maker_fee
                        else:
                            profit_loss_rate -= self.instrument.taker_fee

                        if trade.get_stats()['exit-maker']:
                            profit_loss_rate -= self.instrument.maker_fee
                        else:
                            profit_loss_rate -= self.instrument.taker_fee

                        # and notify
                        self.strategy.notify_order(trade.id, trade.close_direction(), self.instrument.market_id,
                                self.instrument.format_price(close_exec_price), timestamp, trade.timeframe,
                                'stop-loss', profit_loss_rate)

                        # and for streaming
                        if self._global_streamer:
                            self._global_streamer.member('sell-exit' if trade.direction < 0 else 'buy-exit').update(close_exec_price, timestamp)

        self.unlock()

        #
        # remove terminated, rejected, canceled and empty trades
        #

        mutated = False

        self.lock()

        for trade in self.trades:
            if trade.can_delete():
                mutated = True

                # cleanup if necessary before deleting the trade related refs
                trade.remove(trader)

                # record the trade for analysis and study
                if not trade.is_canceled():
                    # @todo all this part could be in an async method of another background service, because 
                    # it is not part of the trade managemnt neither strategy computing, its purely for reporting
                    # and view then we could add a list of the deleted trade (producer) and having another service (consumer) doing the rest

                    # estimation on mid last price, but might be close market price
                    # rate = (trade.best_price() - trade.entry_price) / trade.entry_price  # for best missed profit
                    # rate = (trade.exit_price - trade.entry_price) / trade.entry_price
                    rate = trade.profit_loss  # realized profit/loss

                    # fee rate for entry and exit
                    if trade._stats['entry-maker']:
                        rate -= self.instrument.maker_fee
                    else:
                        rate -= self.instrument.taker_fee

                    if trade._stats['exit-maker']:
                        rate -= self.instrument.maker_fee
                    else:
                        rate -= self.instrument.taker_fee

                    # estimed commission fee rate (futur, stocks)
                    # @todo

                    # perf sommed here it means that its not done during partial closing
                    if rate != 0.0:
                        self._stats['perf'] += rate
                        self._stats['worst'] = min(self._stats['worst'], rate)
                        self._stats['best'] = max(self._stats['best'], rate)

                    if rate <= 0.0:
                        self._stats['cont-loss'] += 1
                        self._stats['cont-win'] = 1

                    elif rate > 0.0:
                        self._stats['cont-loss'] = 0
                        self._stats['cont-win'] += 1

                    record = {
                        'id': trade.id,
                        'ts': trade.entry_open_time,
                        'd': trade.direction_to_str(),
                        'p': self.instrument.format_price(trade.entry_price),
                        'q': self.instrument.format_quantity(trade.order_quantity),
                        'e': self.instrument.format_quantity(trade.exec_entry_qty),
                        'x': self.instrument.format_quantity(trade.exec_exit_qty),
                        'tp': self.instrument.format_price(trade.take_profit),
                        'sl': self.instrument.format_price(trade.stop_loss),
                        'tf': timeframe_to_str(trade.timeframe),
                        'aep': self.instrument.format_price(trade.entry_price),
                        'axp': self.instrument.format_price(trade.exit_price),
                        's': trade.state_to_str(),
                        'b': self.instrument.format_price(trade.best_price()),
                        'w': self.instrument.format_price(trade.worst_price()),
                        'bt': trade.best_price_timestamp(),
                        'wt': trade.worst_price_timestamp(),
                        'rate': rate,
                        'c': trade.get_conditions()
                    }

                    if rate < 0:
                        self._stats['failed'].append(record)
                    elif rate > 0:
                        self._stats['success'].append(record)
                    else:
                        self._stats['roe'].append(record)

        # recreate the list of trades
        if mutated:
            trades_list = []

            for trade in self.trades:
                if not trade.can_delete():
                    # keep only active and pending trades
                    trades_list.append(trade)

            self.trades = trades_list

        self.unlock()

    #
    # region management
    #

    def add_region(self, region):
        self.lock()
        region.set_id(self._next_region_id)
        self._next_region_id += 1
        self.regions.append(region)
        self.unlock()

    def remove_region(self, region_id):
        self.lock()

        for region in self.regions:
            if region.id == region_id:
                self.regions.remove(region)
                
                self.unlock()
                return True

        self.unlock()

        return False

    def cleanup_regions(self, timestamp, bid, ofr):
        """
        Regenerate the list of regions by removing the expired regions.
        @warning Non thread-safe but must be protected.
        """
        regions = []

        for region in self.regions:
            if not region.can_delete(timestamp, bid, ofr):
                regions.append(region)

        # replace the regions list
        self.regions = regions

    def check_regions(self, timestamp, bid, ofr, signal, allow=True):
        """
        Compare a signal to defined regions if somes are defineds.
        @param signal float Signal to check with any regions.
        @param bid float Last instrument bid price
        @param ofr flaot Last instrument ofr price
        @param allow Default returned value if there is no defined region (default True).

        @warning Non thread-safe but must be protected.
        """
        if self.regions:
            mutated = False

            # one ore many region, have to pass at least one test
            for region in self.regions:
                if region.can_delete(timestamp, bid, ofr):
                    mutated |= True

                elif region.test_region(timestamp, signal):
                    # match with at least one region
                    return True

            if mutated:
                self.cleanup_regions(timestamp, bid, ofr)

            return False
        else:
            # no region always pass
            return allow

    #
    # miscs
    #

    def update_trailing_stop(self, trade, instrument, distance, local=True, distance_in_percent=True):
        """
        Update the stop price of a trade using a simple level distance or percent distance method.
        @param local boolean True mean only modify the stop-loss price on this side,
            not on the position or on the stop order

        @note This method is not a way to process a stop, it mostly failed, close for nothing at a wrong price.
        """
        close_exec_price = instrument.close_exec_price(trade.direction)
        stop_loss = trade.sl

        if trade.direction > 0:
            # long case
            ratio = close_exec_price / trade.entry_price
            sl_ratio = (trade.entry_price - trade.sl) / trade.entry_price
            dist = (close_exec_price - trade.sl) / trade.entry_price
            step = distance

            if distance_in_percent:
                # @todo
                if dist > (sl_ratio + step):
                    stop_loss = close_exec_price * (1.0 - distance)
            else:
                # @todo
                pass

            # # if dist > (sl_ratio + step):
            # #     stop_loss = close_exec_price * (1.0 - sl_ratio)
            # #     logger.debug("update SL from %s to %s" % (trade.sl, stop_loss))

            # # # alternative @todo how to trigger
            # # if ratio >= 1.10:
            # #     stop_loss = max(trade.sl, close_exec_price - (close_exec_price/trade.entry_price*(close_exec_price-trade.entry_price)*0.33))

            # # ultra large and based on the distance of the price
            # # if dist > 0.25:
            # #     stop_loss = trade.entry_price + (trade.entry_price * (dist * 0.5))

        elif trade.direction < 0:
            # short case
            ratio = close_exec_price / trade.entry_price
            sl_ratio = (trade.sl - trade.entry_price) / trade.entry_price
            dist = (trade.sl - close_exec_price) / trade.entry_price
            step = distance

            if distance_in_percent:
                # @todo
                if dist > (sl_ratio - step):
                    stop_loss = close_exec_price * (1.0 - distance)
                pass
            else:
                # @todo
                pass

        if stop_loss != trade.sl:
            if local:
                trade.sl = stop_loss
            else:
                trade.modify_stop_loss(trader, instrument.market_id, stop_loss)

    #
    # signal data streaming for profiling
    #

    def create_chart_streamer(self, timeframe):
        """
        Create a streamer for the chart at a specific timeframe.
        Must be overrided.
        """
        return None

    def subscribe(self, timeframe):
        """
        Use or create a specific streamer.
        """
        result = False
        self.lock()

        if timeframe is not None and isinstance(timeframe, (float, int)):
            timeframe = self.timeframes.get(timeframe)

        if timeframe in self._timeframe_streamers:
            self._timeframe_streamers[timeframe].use()
            result = True
        else:
            streamer = self.create_chart_streamer(timeframe)

            if streamer:
                streamer.use()
                self._timeframe_streamers[timeframe] = streamer
                result = True

        self.unlock()
        return False

    def unsubscribe(self, timeframe):
        """
        Delete a specific streamer when no more subscribers.
        """
        result = False
        self.lock()

        if timeframe is not None and isinstance(timeframe, (float, int)):
            timeframe = self.timeframes.get(timeframe)

        if timeframe in self._timeframe_streamers:
            self._timeframe_streamers[timeframe].unuse()
            if self._timeframe_streamers[timeframe].is_free():
                # delete if 0 subscribers
                del self._timeframe_streamers[timeframe]
    
            result = True

        self.unlock()
        return False

    def stream_call(self):
        """
        Process the call for the strategy trader. Must be overriden.
        """
        pass
