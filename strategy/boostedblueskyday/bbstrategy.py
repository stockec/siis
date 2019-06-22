# @date 2018-08-24
# @author Frederic SCHERMA
# @license Copyright (c) 2018 Dream Overflow
# Blue Sky Day boosted strategy

import time
import datetime
import copy

import numpy as np

from terminal.terminal import Terminal
from watcher.position import Position as WatcherPosition
from trader.position import Position as TraderPosition
from trader.order import Order

from config import config
from strategy.strategy import Strategy

from notifier.notifiable import Notifiable
from notifier.signal import Signal

from instrument.instrument import Instrument, Candle
from watcher.watcher import Watcher

from charting.charting import Charting
from database.database import Database

import logging
logger = logging.getLogger('siis.strategy.boostedblueskyday')

# @deprecated !!!!

class BoostedBlueSkyDayStrategyTrader(object):
    """
    @todo need to bootstrap in live mode with the last N values to compute the previous scores to have a better accuracy and relevance at startup
    """

    MIN_SCORE = 3
    BEST_SCORE = 5

    def __init__(self, strategy, instrument):
        self.strategy = strategy
        self.instrument = instrument

        # signals long or short with score. 0 mean no signal, negative signal mean short
        self.buy_sell_signals = []

        # last n signals for the two indicators
        self.blueskyday = []
        self.channelbreakout = []

        self.chart = None

        # indicators
        self.rsi = self.strategy.indicator('rsi')(14, 1, False)
        self.sma = self.strategy.indicator('sma')(7, 1, False)
        self.ema = self.strategy.indicator('ema')(9, 1, False)
        # self.hma = self.strategy.indicator('hma')(7, 1, False) @todo how ?
        self.vwma = self.strategy.indicator('vwma')(25, 1, False)
        # self.srsi = self.strategy.indicator('srsi')(7, 1, False) @todo
        # self.mmt = self.strategy.indicator('momentum')(20, 1, False) @todo test it
        # self.bollinger = self.strategy.indicator('bollinger')(26, 1, False) @todo
        # self.macd = self.strategy.indicator('macd')(17, 1, False) @todo

        # for backtesting
        self.candles = {}
        self.next_candle = {}

        self.scores = [0]

        self.longs = []   # history for charting and stats
        self.shorts = []

        self.cur_score = 0  # last score validated

    def ready(self):
        # ready once all the candles are availables
        if (self.candles.get(Instrument.TF_MIN) is not None and
            self.candles.get(Instrument.TF_5MIN) is not None and
            self.candles.get(Instrument.TF_HOUR) is not None):
            return True

        return False


class BoostedBlueSkyDayStrategy(Strategy):
    """
    Blue Sky Day boosted strategy.
    """

    def __init__(self, strategy_service, watcher_service, trader_service, options, parameters):
        super().__init__("boostedblueskyday", strategy_service, watcher_service, trader_service, options, DEFAULT_PARAMS)

        if parameters:
            # apply overrided parameters
            self._parameters.update(parameters)

        # per instrument strategy data analyser
        self._sub_traders = {}

    def reset(self):
        # per instrument strategy sub-trader
        self._sub_traders = {}
        self._last_done_ts = 0

        # reversal mode is default, else need to define how to prefer entry or exit
        self._reversal = True
        
        # mean when there is already a position on the same direction does not increase in the same direction if 0
        # or increase at max N times
        self._pyramided = 0

        self.tf = 60  # process at 1 min

        # depth of last data taken, this is the max window width
        self.depth = 48  # of 1 min candles (48 120 480)

        # minimal depth but the minimal must always be greater than the max length of the used indicators
        self.min_depth = 14+14+1  # 25 # +14  # RSI 14 +1

    def start(self):
        if super().start():
            # rest data
            self.reset()

            # listen to watchers and strategy signals
            self.watcher_service.add_listener(self)
            self.service.add_listener(self)

            return True
        else:
            return False

    def pause(self):
        super().pause()

    def stop(self):
        super().stop()

        # rest data
        self.reset()

    def create_trader(self, instrument):
        return BoostedBlueSkyDayStrategyTrader(self, instrument, self.parameters)

    def setup_live(self):
        # pre-feed en live mode only
        Terminal.inst().info("> In appliance %s retrieves last data history..." % self.name, True)
        max_retry = 30

        # retrieve recent data history
        for market_id, instrument in self._instruments.items():
            try:
                price_and_vol_watcher = instrument.watcher(Watcher.WATCHER_PRICE_AND_VOLUME)
                if price_and_vol_watcher:
                    # query for most recent candles
                    price_and_vol_watcher.historical_data(market_id, Instrument.TF_MIN, n_last=720)
                    # price_and_vol_watcher.historical_data(market_id, Instrument.TF_5MIN, n_last=120)
                    # price_and_vol_watcher.historical_data(market_id, Instrument.TF_HOUR, n_last=4*24)

                    # buy/sells signals
                    # @todo
            except Exception as e:
                Terminal.inst().error(repr(e))
                import traceback
                logger.error(traceback.format_exc())

        Terminal.inst().info("> Appliance data retrieved")

    def update_strategy(self, tf, instrument):
        if self._reversal:
            self.__update_reversal(instrument)
        else:
            self.__update_entry_exit(instrument)

    def __update_reversal(self, instrument):
        # consts
        MIN_SCORE = 4*60   # min score to reach to validate an order
        BSD_SCORE_FACTOR = 2  # 2,3
        CBO_SCORE_FACTOR = 2  # 2,3
        RSI_SCORE_FACTOR = 0.5  # 0.5,1,2
        RSI_TREND_SCORE_FACTOR = 1  # 0.5,1,2,4
        EMA_VWMA_CROSS_SCORE_FACTOR = 8000  # 5000,8000
        VWMA_PRICE_CROSS_SCORE_FACTOR = 2000  # 2000,4000
        EMA_VWM_BONUS_SCORE = 2  # 1,2,3,5
        TIME_SCORE_REGRESSION_FACTOR = 0.75  # 0.375,0.5,0.75
        RSI_LOW = 30  # 25,30,35
        RSI_HIGH = 70  # 65,70,75

        # @todo a plot of the account balance and % gain/loss of each trade

        # process in 1 minute, retrieve analysis data instrument
        sub_trader = self._sub_traders.get(instrument)

        # compute with the max samples
        num_samples = instrument.num_samples(Instrument.TF_MIN)
        depth = min(self.depth, num_samples)
        last_prices = instrument.last_prices(Instrument.TF_MIN, Instrument.PRICE_CLOSE, depth)
        last_volumes = instrument.last_volumes(Instrument.TF_MIN, depth)

        # current timestamp
        timestamp = self.timestamp

        # instrument.last_candles(Instrument.TF_MIN, depth)
        # @todo Typical price is attained by taking adding the high, low and close, and dividing by three: (H+L+C)/3

        if depth < self.min_depth:
            # not enought samples
            return

        rsi = sub_trader.rsi.compute(last_prices)
        sma = sub_trader.sma.compute(last_prices)
        ema = sub_trader.ema.compute(last_prices)
        vwma = sub_trader.vwma.compute(last_prices, last_volumes)

        #
        # scorify
        #

        bsd_score = 0
        cbo_score = 0
        rsi_score = 0
        ema_vwma_score = 0
        ema_vwma_bonus_score = 0
        price_vwma_score = 0

        if sub_trader.blueskyday:
            if sub_trader.blueskyday[-1].direction == Position.POSITION_LONG:
                bsd_score = BSD_SCORE_FACTOR
            elif sub_trader.blueskyday[-1].direction == Position.POSITION_SHORT:
                bsd_score = -BSD_SCORE_FACTOR

        if sub_trader.channelbreakout:
            if sub_trader.channelbreakout[-1].direction == TraderPosition.POSITION_LONG:
                cbo_score = CBO_SCORE_FACTOR
            elif sub_trader.channelbreakout[-1].direction == TraderPosition.POSITION_SHORT:
                cbo_score = -CBO_SCORE_FACTOR

        # rsi 30/70, gives strong signals
        # @todo be we could compute it on two tf (the last 14N and the more global at depth level to have two trends)
        rsi_argmin = np.argmin(rsi)
        rsi_argmax = np.argmax(rsi)

        # trend of the rsi @todo linear regression or MM
        if rsi_argmin < rsi_argmax and rsi[rsi_argmin] < rsi[rsi_argmax]:  # ++
            rsi_trend = (rsi[rsi_argmax] + rsi[rsi_argmin]) / (rsi[rsi_argmax] - rsi[rsi_argmin])
        elif rsi_argmax < rsi_argmin and rsi[rsi_argmax] > rsi[rsi_argmin]:  ## --
            rsi_trend = (rsi[rsi_argmin] + rsi[rsi_argmax]) / (rsi[rsi_argmin] - rsi[rsi_argmax])
        else:
            rsi_trend = 0

        if rsi[-1] < RSI_LOW:
            rsi_score = (RSI_LOW-rsi[-1]) * RSI_SCORE_FACTOR  # ++
            if rsi_trend > 0:
                rsi_score += rsi_trend * RSI_TREND_SCORE_FACTOR
        elif rsi[-1] > RSI_HIGH:
            rsi_score = (RSI_HIGH-rsi[-1]) * RSI_SCORE_FACTOR
            if rsi_trend < 0:
                rsi_score += rsi_trend * RSI_TREND_SCORE_FACTOR

        # prev = rsi[0]
        # for (i, v) in enumerate(rsi):
        #   if v < prev and v < RSI_LOW:
        #       longs.append((i, last_prices[i]))
        #       prev = v
        #   elif v > prev and v > RSI_HIGH:
        #       shorts.append((i, last_prices[i]))
        #       prev = v

        # ema/vwma crossing
        ema_vwma_score = (ema[-1]-vwma[-1]) / last_prices[-1] * EMA_VWMA_CROSS_SCORE_FACTOR

        # vwma/price crossing
        price_vwma_score = (last_prices[-1]-vwma[-1]) / last_prices[-1] * VWMA_PRICE_CROSS_SCORE_FACTOR

        # if last_prices[-1] > vwma[-1]:
        #   sub_trader.scores[-1] += 1
        # elif last_prices[-1] < vwma[-1]:
        #   sub_trader.scores[-1] -= 1

        # ema/vwma crossing and vwmap/price more score !!
        if ema[-1] > vwma[-1] and last_prices[-1] > vwma[-1]:
            ema_vwma_bonus_score = EMA_VWM_BONUS_SCORE
        elif ema[-1] < vwma[-1] and last_prices[-1] < vwma[-1]:
            ema_vwma_bonus_score = -EMA_VWM_BONUS_SCORE

        # support/resistance signal
        # @todo and then scores +-= 2

        # price delta min including spread, have to determine if the price can vary of a minimal size
        # @todo

        # confirmation on N candles, and don't take care of pyramided orders
        total_score = rsi_score + ema_vwma_score + price_vwma_score + ema_vwma_bonus_score + bsd_score + cbo_score

        #
        # score tuning
        #

        # store the total score
        sub_trader.scores[-1] = total_score
        final_score = total_score

        # average of the two last score and increase the last, score is exp if signals are in the trend
        if len(sub_trader.scores) > 2:
            final_score = np.average(sub_trader.scores[-2:])
            final_score += sub_trader.scores[-2]

        # and store it
        sub_trader.scores[-1] = final_score

        # handle a score convergence to avoid multiple signals
        if (sub_trader.cur_score > 0 and final_score > 0) or (sub_trader.cur_score < 0 and final_score < 0):
            # cancel all
            # sub_trader.scores = [0]

            # or ignore
            # sub_trader.scores[-1] = 0

            # or take 75% of the previous score to minimize its impact progressively
            # sub_trader.scores[-1] = sub_trader.scores[-2] * TIME_SCORE_REGRESSION_FACTOR

            # or keep only 37.5% of it
            sub_trader.scores[-1] *= TIME_SCORE_REGRESSION_FACTOR * 0.5

            # keep as final score or nullify
            final_score = sub_trader.scores[-1]

        # handle a score divergence
        # if (rsi_score > 0 and ema_vwma_score < 0) or (rsi_score < 0 and ema_vwma_bonus_score > 0):
        #       total_score *= 0.25

        # limit sub_trader.scores len to max depth
        if len(sub_trader.scores) > self.depth:
            sub_trader.scores = sub_trader.scores[len(sub_trader.scores)-self.depth:]

        #
        # pass an order if score is accepted
        #

        if abs(final_score) >= MIN_SCORE:
            # keep apart the current score
            sub_trader.cur_score = final_score

            if final_score > 0:
                sub_trader.longs.append((timestamp, last_prices[-1]))
            elif final_score < 0:
                sub_trader.shorts.append((timestamp, last_prices[-1]))

            date_str = datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
            direction = TraderPosition.POSITION_LONG if final_score > 0 else TraderPosition.POSITION_SHORT

            # create an order an post it
            if final_score > 0:
                Terminal.inst().notice("> Strategy %s LONG %s%s at price %.4f on %s" % (self.name, instrument.trader_quantity, instrument.market_id, last_prices[-1], date_str))
            else:
                Terminal.inst().notice("> Strategy %s SHORT %s%s at price %.4f on %s" % (self.name, instrument.trader_quantity, instrument.market_id, last_prices[-1], date_str))

            trader = self.trader()
            if trader:
                order = Order(trader, instrument.market_id)
                order.direction = direction
                order.order_price = last_prices[-1]

                # depends of the instrument and the account, and not always necessary, but always in paper trader
                order.leverage = instrument.leverage

                positions = trader.positions(instrument.market_id)

                current_opposite_qty = 0.0
                current_same_qty = 0.0

                for position in positions:
                    # strategy does a reversal (and quantity are always positives)
                    if position.direction != direction:
                        # opposit directions ?
                        current_opposite_qty += position.quantity  # need to close that
                    else:
                        # or same direction ?
                        current_same_qty += position.quantity

                # trading quantity + what we have in opposite direction - what we already have in the same direction
                if self._pyramided >= 1:
                    order.quantity = instrument.trader_quantity + current_opposite_qty - current_same_qty  # @todo
                else:
                    order.quantity = instrument.trader_quantity + current_opposite_qty - current_same_qty

                if order.quantity > 0:
                    # @todo debug only
                    Terminal.inst().info("Do order %s %s with %s" % (instrument.market_id, 'SHORT' if direction==TraderPosition.POSITION_SHORT else 'LONG', order.quantity))
                    trader.create_order(order)

            # consumes buy sell signals
            # @todo could put previous scores into history
            # sub_trader.scores = [0]
            sub_trader.blueskyday = []
            sub_trader.channelbreakout = []
        else:
            # append the next score entry at 0
            sub_trader.scores.append(0)

        #
        # charting
        #

        if sub_trader.chart is None and Charting.inst():
            # create the chart if necessary
            sub_trader.chart = Charting.inst().chart("%s on %s" % (self.name, instrument.symbol))

        rechart = sub_trader.chart.can_redraw

        if rechart:
            longs = []
            shorts = []

            # take only in depth longs and shorts
            for long in sub_trader.longs:
                if long[0] + depth*Instrument.TF_MIN >= timestamp:
                    longs.append(((long[0] + depth*Instrument.TF_MIN - timestamp) / Instrument.TF_MIN, long[1]))

            for short in sub_trader.shorts:
                if short[0] + depth*Instrument.TF_MIN >= timestamp:
                    shorts.append(((short[0] + depth*Instrument.TF_MIN - timestamp) / Instrument.TF_MIN, short[1]))

            # @todo send a stream with the last values or/and updated ranges/objects
            sub_trader.chart.set_range(0, depth)

            sub_trader.chart.plot_price_serie(0, last_prices)
            sub_trader.chart.plot_price_serie(1, sma)
            sub_trader.chart.plot_price_serie(2, ema)
            sub_trader.chart.plot_price_serie(3, vwma)
            sub_trader.chart.annotate_price(0, longs, 'g^')
            sub_trader.chart.annotate_price(1, shorts, 'r^')

            sub_trader.chart.plot_serie(1, 0, rsi)
            sub_trader.chart.plot_serie(1, 1, [30]*len(rsi))
            sub_trader.chart.plot_serie(1, 2, [70]*len(rsi))
            # sub_trader.chart.plot_serie(2, 0, mmt)
            sub_trader.chart.draw()

    def __update_entry_exit(self, instrument):
        s1 = "blueskyday"
        s2 = "macrossover"

        match = False
        do_order = True
        entry_or_exit = True

        # @todo

        # if signal_data.order_type == BuySellSignal.ORDER_ENTRY:
        #   pass

        # if signal_data.order_type == BuySellSignal.ORDER_EXIT:
        #   pass

        if entry_or_exit:
            # @todo entry position
            pass
        else:
            # @todo exit position
            pass

    def ready(self):
        # if not self.is_alive():
        if not self.running:            
            return False

        self.lock()
        ready = True

        for market_id, instrument in self._instruments.items():
            sub_trader = self._sub_traders.get(instrument)
            if not sub_trader.ready():
                ready = False
                break

        self.unlock()
        return ready

    def setup_backtest(self, from_date, to_date):
        trader = self.trader()

        # prealod data for any supported instruments
        for market_id, instrument in self._instruments.items():
            sub_trader = self._sub_traders.get(instrument)

            watcher = instrument.watcher(Watcher.WATCHER_PRICE_AND_VOLUME)

            Database.inst().load_market_info(self.service, watcher.name, market_id)

            Database.inst().load_market_ohlc(self.service, watcher.name, market_id, Instrument.TF_MIN, from_datetime=from_date, to_datetime=to_date)
            Database.inst().load_market_ohlc(self.service, watcher.name, market_id, Instrument.TF_5MIN, from_datetime=from_date, to_datetime=to_date)
            Database.inst().load_market_ohlc(self.service, watcher.name, market_id, Instrument.TF_HOUR, from_datetime=from_date, to_datetime=to_date)

            # and buy/sell signals for blueskyday (sparse data)
            # @todo need storage at watcher level
            # buy_sells_min = []
            # buy_sells_five_min = []
            # buy_sells_hour = []

            # init next candle index
            sub_trader.next_candle[Instrument.TF_MIN] = 0
            sub_trader.next_candle[Instrument.TF_5MIN] = 0
            sub_trader.next_candle[Instrument.TF_HOUR] = 0

            sub_trader.count = 0

    def backtest_update(self, timestamp, total_ts):
        for market_id, instrument in self._instruments.items():
            sub_trader = self._sub_traders.get(instrument)
            do_update = False

            if sub_trader.next_candle.get(Instrument.TF_MIN) is not None and sub_trader.candles.get(Instrument.TF_MIN):
                while sub_trader.next_candle[Instrument.TF_MIN] < len(sub_trader.candles[Instrument.TF_MIN]):
                    if sub_trader.candles[Instrument.TF_MIN][sub_trader.next_candle[Instrument.TF_MIN]].timestamp <= timestamp:
                        instrument.add_candle(sub_trader.candles[Instrument.TF_MIN][sub_trader.next_candle[Instrument.TF_MIN]])

                        sub_trader.next_candle[Instrument.TF_MIN] += 1
                        do_update = True
                    else:
                        break

            if sub_trader.next_candle.get(Instrument.TF_5MIN) is not None and sub_trader.candles.get(Instrument.TF_5MIN):                       
                while sub_trader.next_candle[Instrument.TF_5MIN] < len(sub_trader.candles[Instrument.TF_5MIN]):
                    if sub_trader.candles[Instrument.TF_5MIN][sub_trader.next_candle[Instrument.TF_5MIN]].timestamp <= timestamp:
                        instrument.add_candle(sub_trader.candles[Instrument.TF_5MIN][sub_trader.next_candle[Instrument.TF_5MIN]])

                        sub_trader.next_candle[Instrument.TF_5MIN] += 1
                        do_update = True
                    else:
                        break                   

            if sub_trader.next_candle.get(Instrument.TF_HOUR) is not None and sub_trader.candles.get(Instrument.TF_HOUR):
                while sub_trader.next_candle[Instrument.TF_HOUR] < len(sub_trader.candles[Instrument.TF_HOUR]):
                    if sub_trader.candles[Instrument.TF_HOUR][sub_trader.next_candle[Instrument.TF_HOUR]].timestamp <= timestamp:
                        instrument.add_candle(sub_trader.candles[Instrument.TF_HOUR][sub_trader.next_candle[Instrument.TF_HOUR]])

                        sub_trader.next_candle[Instrument.TF_HOUR] += 1
                        do_update = True
                    else:
                        break                   

            # current processing timestamp
            self._timestamp = timestamp

            if do_update:
                trader = self.trader()

                if not trader.has_market(instrument.market_id):
                    continue

                # update the market instrument data before processing, but we does not have the exact base exchange rate so currency converted
                # prices on backtesting are informals
                trader.on_update_market(instrument.market_id, True, timestamp, instrument.bid(), instrument.ofr(), instrument.base_exchange_rate)

                if self._reversal:
                    self.__update_reversal(instrument)
                else:
                    self.__update_entry_exit(instrument)

            # for progression
            self._last_done_ts = timestamp
