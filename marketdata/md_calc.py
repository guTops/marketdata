# -*- coding: UTF-8 -*-

import time
import redis
from redis import WatchError

from RedisAdvMdResolver import *
from depthMD import depthMD
from utils import Configuration, parse_conf_args, log, pubsub


class start_md_calc:
    ADV_KEY_PREFIX = ":ADV:"

    def __init__(self, context, conf):
        self.logger = log.get_logger(category="MdCalc")
        self.logger.info(
            "[start stock md calculate with %s] begin" % (json.dumps(conf, encoding="UTF-8", ensure_ascii=False)))

        # 建立redis RAW库
        redis_conf = context.get("redis").get(conf.get("redis_row"))
        pool = redis.ConnectionPool(host=redis_conf.get('host'),
                                    port=redis_conf.get('port'),
                                    password=redis_conf.get('password'),
                                    db=redis_conf.get('db'))
        self.redis_raw = redis.Redis(connection_pool=pool)

        # 建立redis ADV库
        redis_conf = context.get("redis").get(conf.get("redis_adv"))
        pool = redis.ConnectionPool(host=redis_conf.get('host'),
                                    port=redis_conf.get('port'),
                                    password=redis_conf.get('password'),
                                    db=redis_conf.get('db'))
        self.redis_adv = redis.Redis(connection_pool=pool)

        # 建立zmq消息服务器
        sources = conf.get("sourceMQ")
        xmq_info = context.get("xmq")
        msg_queue_pusher = dict()
        for source in sources:
            addr = xmq_info.get(source).get("address")
            topic = xmq_info.get(source).get("topic")
            ps = pubsub.QueuePublisher(addr=addr, topic=topic)
            msg_queue_pusher.update({source: ps})

        # 参数
        self.step = conf.get("calc_step")
        self.exchange = conf.get("exchange")
        self.sgid = conf.get("settlementgroup")

        # 判断是否有交易时间
        while True:
            tradingDay = self.redis_adv.get(self.sgid + ":Exchange:TradingDay")
            if tradingDay is not None:
                self.tradingDay = tradingDay
                self.logger.info("tradingDay: %s", tradingDay)
                break
            time.sleep(1)

        self.adv = RedisAdvMdResolver(redis_raw=self.redis_raw,
                                      redis_adv=self.redis_adv,
                                      sgid=self.sgid,
                                      exchange=self.exchange,
                                      tradingDay=self.tradingDay,
                                      xmq=msg_queue_pusher)

        # 查询合约
        self.instrument_loop()

    def instrument_loop(self):
        # 获取tradingDay
        tradingDay = self.tradingDay

        # 循环处理
        self.logger.info(" ======= 循环计算Begin ======= ")

        advKeyPrefix = self.sgid + ":" + tradingDay + ":ADV:Security"
        rawKeyPrefix = self.sgid + ":" + tradingDay + ":RAW:Security"

        while True:
            # 计算当前进程计算合约范围
            instrument_list = self.inc_and_get()
            # 开始计算数据
            for security in instrument_list:
                # ADV_List 与 RAW_List 差集为需要计算的数据
                # 获取最后一条记录查看adv计算到哪一时间
                lastAdv = self.redis_adv.zrange("%s%s%s%s" % (advKeyPrefix, ":", security, ":LS_MD_List"), -1, -1,
                                                   withscores=True)
                last_modified_key = "%s%s%s%s" % (advKeyPrefix, ":", security, ":Last_Modified")
                # 没有数据查询全部
                lastAdvTime = 0
                if len(lastAdv) != 0:
                    lastAdvTime = lastAdv[0][1]

                ret_list = self.redis_raw.zrangebyscore(
                    name="%s%s%s%s" % (rawKeyPrefix, ":", security, ":LS_MD:List"),
                    min="(" + str(lastAdvTime), max="+inf", withscores=True)
                if len(ret_list) == 0:
                    # 获取该合约的缓存时间（判断是否大于一分钟）
                    last_modified = self.redis_adv.get(last_modified_key)
                    if last_modified is None:
                        self.redis_adv.set(last_modified_key, self.redis_adv.time()[0])
                        diff = 0
                    else:
                        diff = int(self.redis_adv.time()[0]) - int(last_modified)
                    # 计算大于一分钟数据
                    if diff > 60:
                        # 更新缓存记录时间
                        self.redis_adv.set(last_modified_key, self.redis_adv.time()[0])
                        self.logger.info(security + "超过一分钟，写入数据")
                        last_min_md = self.redis_adv.zrange(
                            name="%s%s%s%s" % (advKeyPrefix, ":", security, ":MI_MD"), start=-1, end=-1,
                            withscores=True)
                        if len(last_min_md) != 0:
                            last_min_md_score = '{:.0f}'.format(last_min_md[0][1])
                            # 3) 获取最近一条RAW数据的KEY
                            lastMD = \
                            self.redis_raw.zrange("%s%s%s%s" % (rawKeyPrefix, ":", security, ":LS_MD:List"), -1, -1)[0]
                            md = depthMD(self.redis_raw.hgetall(lastMD))

                            # 将时间加一分钟
                            localscore = last_min_md_score[8:14]
                            date_time = datetime.datetime.strptime(localscore, "%H%M%S")
                            localscore = (date_time + datetime.timedelta(seconds=60 - int(localscore[4:6]))).strftime(
                                "%H%M%S")

                            # 修改md的时间
                            md.UpdateTime = localscore[0:2] + ":" + localscore[2:4] + ":" + localscore[4:6]
                            md.UpdateMillisec = "000"

                            # 先判断股票是否结束
                            tradingTime = self.redis_adv.zrange(
                                "%s%s%s%s" % (advKeyPrefix, ":", security, ":TradingTime"),
                                0, -1)
                            isTrading = False
                            UpdateTime = tradingDay + localscore + str(md.UpdateMillisec).zfill(3)
                            for timeSpot in tradingTime:
                                timeSpot = json.loads(timeSpot)
                                if long(str(timeSpot["KS"]) + "000") <= long(UpdateTime) <= long(
                                        str(timeSpot["JS"]) + "000"):
                                    isTrading = True
                            # 不在交易时间段内则跳走
                            if not isTrading:
                                continue
                            self.adv.unify_md(md)
                else:
                    # 更新所有ADV和RAW差值数据
                    tradingTime = self.redis_adv.zrange("%s%s%s%s" % (advKeyPrefix, ":", security, ":TradingTime"), 0,-1)
                    for un in ret_list:
                        # 更新缓存记录时间
                        self.redis_adv.set(last_modified_key, self.redis_adv.time()[0])
                        # 计算RAW过后的数据存入ADV中，之后只需要比较RAW与ADV差异即可
                        ADVListKey = advKeyPrefix + ":" + security + ":LS_MD_List"
                        self.redis_adv.zadd(ADVListKey, un[0], un[1])
                        # 查询当前RAW数据
                        md = depthMD(self.redis_raw.hgetall(un[0]))
                        # 先判断股票是否结束
                        isTrading = False
                        UpdateTime = tradingDay + md.UpdateTime.replace(":", "") + str(md.UpdateMillisec).zfill(3)
                        for timeSpot in tradingTime:
                            timeSpot = json.loads(timeSpot)
                            if long(str(timeSpot["KS"]) + "000") <= long(UpdateTime) <= long(str(timeSpot["JS"]) + "000"):
                                isTrading = True
                        # 不在交易时间段内则跳走
                        if not isTrading:
                            continue
                        # 获取UpdateTime的时间作为score
                        score = long(self.calcMDdate(tradingDay + md.UpdateTime.replace(":", ""), str(md.UpdateMillisec)))
                        # 和分钟时间计算相差数值
                        last_min_md = self.redis_adv.zrange(name="%s%s%s%s" % (advKeyPrefix, ":", security, ":MI_MD"), start=-1, end=-1, withscores=True)
                        if len(last_min_md) > 0:
                            last_min_md_score = '{:.0f}'.format(last_min_md[0][1])
                            new_time = datetime.datetime.strptime(str(score)[8:12], "%H%M")
                            old_time = datetime.datetime.strptime(last_min_md_score[8:12], "%H%M")
                            diff = int((new_time - old_time).total_seconds() / 60)
                        else:
                            diff = 1

                        for i in range(1, diff):
                            # 计算行情信息
                            add_time = (old_time + datetime.timedelta(minutes=i)).strftime("%H%M")
                            md.UpdateTime = str(add_time)[0:2] + ":" + str(add_time)[2:4] + ":00"
                            md.UpdateMillisec = "000"
                            self.adv.resolve_minute_md(md)
                        # 计算行情信息
                        self.adv.resolve_instrument_md(md)
                        self.adv.unify_md(md)

    def inc_and_get(self):
        current_index_key = self.sgid + ":" + self.tradingDay + self.ADV_KEY_PREFIX + "Security:Current"
        instrument_list_key = self.sgid + ":" + self.tradingDay + self.ADV_KEY_PREFIX + "Security:List"
        with self.redis_adv.pipeline(transaction=True) as pipe:
            while True:
                try:
                    pipe.watch(current_index_key)
                    range_start = int(pipe.get(current_index_key))
                    pipe.multi()
                    range_start = 1 if range_start is None else range_start
                    range_end = int(range_start) + int(self.step)
                    instrument_list = self.redis_adv.zrangebyscore(instrument_list_key, range_start, range_end - 1)
                    if len(instrument_list) < self.step:
                        range_end = 1
                    pipe.set(current_index_key, range_end)
                    pipe.execute()
                    return instrument_list
                except WatchError, ex:
                    print ex
                    continue
                finally:
                    pipe.reset()

    def calcMDdate(self, date, milliseconds):
        diffMilli = 0
        diffSec = 0
        second = date[12:14]
        if int(milliseconds) != 0:
            diffMilli = 1000 - int(milliseconds)
            diffSec = 1
        if second != "00" or diffSec == 1:
            diffSec = 60 - int(second) - diffSec
        date_time = datetime.datetime.strptime(date + milliseconds, "%Y%m%d%H%M%S%f")
        date = (date_time + datetime.timedelta(seconds=diffSec, milliseconds=diffMilli)).strftime("%Y%m%d%H%M%S")
        return date


def main():
    base_dir, config_names, config_files, add_ons = parse_conf_args(__file__,
                                                                    config_names=["xmq", "redis", "mysql", "exchange"])

    context, conf = Configuration.load(base_dir=base_dir, config_names=config_names, config_files=config_files)

    start_md_calc(context=context, conf=conf)


if __name__ == "__main__":
    main()
