"""仅基于两份完整 profile JSON 的分层读取工具；无数据库、会话或源数据查询。"""
import argparse
import html
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .statistic import quantile, parse_time
from .dimension import dimension_key


DEFAULT_FIELDS = ['finite_numeric_count','null_count','non_numeric_count','non_finite_count',
                  'zero_count','negative_count','min','max','mean','stddev_pop',
                  'p05','p25','p50','p75','p95']


def display(value):
    """把值转成安全表格单元格；长文本标注为预览，不改变文件中的原值。

    原理：文本转义后展示；数值用六位有效数字。SQL 无对应计算。
    用法：用于最终渲染，不能把截短的值反用作精确筛选键。
    """
    if value is None:
        text='NULL'
    elif type(value) is float:
        text=format(value,'.6g')
    elif isinstance(value,(list,dict)):
        text=json.dumps(value,ensure_ascii=False,separators=(',',':'))
    else:
        text=str(value)
    if len(text)>240:
        text=text[:240]+'…[显示截短，完整值保留于JSON]'
    return html.escape(text,quote=False).replace('|','&#124;').replace('\n',' ').replace('\r',' ')


def table(headers, rows):
    """由列名和数据行生成 Markdown 表格；只做呈现，不重新计算 profile。"""
    return '\n'.join(['| '+' | '.join(map(display,headers))+' |',
                      '| '+' | '.join(['---']*len(headers))+' |']+
                     ['| '+' | '.join(map(display,row))+' |' for row in rows])


def distribution_value(distribution, field):
    """读取已有分布字段，分位数从 quantiles 取；不从桶分位数推导窗口分位数。"""
    return distribution.get('quantiles',{}).get(field) if field in ('p05','p25','p50','p75','p95') else distribution.get(field)


def spread(values):
    """对已有组级统计生成 P10/P50/P90；每组等权，与结果行分布明确区分。

    用法：传入全体非空组中位数或行数；原理：排序后 Type 7 插值，空列表返回 NULL。
    MySQL 下推可以对完整组级统计中间表再使用窗口分位数。
    """
    ordered=sorted(v for v in values if v is not None)
    return [quantile(ordered,p) for p in (.1,.5,.9)]


def locate_dimension(profile, names):
    """从已保存的单维/组合中找精确维度集合；不创建未计算的组合。"""
    if len(names)!=len(set(names)):
        raise ValueError('dimension 中维度名不得重复')
    for item in profile['single_dimensions']+profile['combinations']:
        if set(item['dimensions'])==set(names):
            return item
    raise ValueError('该维度集合没有已保存的 profile')


def choose_groups(item, names, values):
    """选择完整组列表或匹配一个类型化维度值元组；NULL、字符串和布尔值不混淆。

    用法：names=['channel'],values=['ads']；values=None 表示全部组。
    原理：比较已有 slice，不筛选或重算源数据；组序号沿用 JSON 数组位置。
    """
    if values is not None and (not isinstance(values,list) or any(v is not None and type(v) not in (str,int,float,bool) for v in values)):
        raise ValueError('values 必须为 JSON 标量数组')
    if values is not None and len(values)!=len(names):
        raise ValueError('values 数量必须与 dimension 一致')
    expected=None if values is None else dict(zip(names,map(dimension_key,values)))
    return [(i,g) for i,g in enumerate(item['groups']) if expected is None or
            all(expected[v['name']]==(v['type'],v['value']) for v in g['slice'])]


def time_buckets(statistic, start, end):
    """选择已保存的原时间桶，范围左闭右开；不合并桶或平均分位数。

    输入日期/时间按 profile 时区解释，返回 (原桶序号,桶) 列表。
    纯日期下界为零点；无时间统计时返回空列表，保留状态由调用者报告。
    """
    time=statistic['time']
    zone=ZoneInfo(time['timezone']) if time.get('timezone') else None
    lo=parse_time(start,zone) if start is not None else None
    hi=parse_time(end,zone) if end is not None else None
    if lo is not None and hi is not None and lo>=hi:
        raise ValueError('start 必须早于 end')
    return [(i,b) for i,b in enumerate(time.get('buckets',[]))
            if (lo is None or parse_time(b['time'],zone)>=lo) and
               (hi is None or parse_time(b['time'],zone)<hi)]


def overview(statistic, dimension, metric_ids, items, buckets):
    """生成全量概况与少量代表桶；返回统一三列表格的数据行。

    维度频次和组间分布使用全部已保存组；时间最多选 24 个原桶，优先覆盖首尾和
    各指标极值所在桶，再补均匀位置。不声称选中桶包含所有现象，不造窗口分位数。
    概况自身过长由 read_profile 分页，原始 profile 不被裁剪。
    """
    result=[['数据','结果行数',statistic['dataset']['result_row_count']],
            ['数据','结果重复额外行',statistic['dataset']['duplicates']['extra_row_count']]]
    for metric in metric_ids:
        d=statistic['metrics'][metric]['distribution']
        result.append(['全局指标',metric,{k:distribution_value(d,k) for k in ('finite_numeric_count','null_count','min','p05','p50','p95','max')}])
    for item in items:
        groups=item['groups'];counts=[g['result_row_count'] for g in groups]
        result.append(['维度概况',item['dimensions'],{'组数':item['group_count'],'非空组数':item['non_null_group_count'],
                       '空值行':item['null_row_count'],'组大小P10/P50/P90':spread(counts),
                       '单例组数':counts.count(1),'Top10行数':sum(sorted(counts,reverse=True)[:10]),
                       '已保存组数':len(groups),'完整':not item['coverage']['truncated']}])
        for metric in metric_ids:
            medians=[g['metrics'][metric]['distribution']['quantiles']['p50'] for g in groups if metric in g['metrics']]
            result.append(['组间分布（每组等权）',str(item['dimensions'])+' / '+metric,
                           {'组中位数P10/P50/P90':spread(medians),'数值组数':sum(v is not None for v in medians)}])
    time=statistic['time']
    result.append(['时间概况',time.get('column','无时间列'),{'status':time['status'],'min':time.get('min'),'max':time.get('max'),
                   '候选桶数':len(buckets),'完整':not time.get('truncated',False),
                   '说明':'代表桶为原桶，不是合并窗口；未返回时间不推断为缺失'}])
    chosen=[]
    def add(i):
        """去重保留代表桶顺序；选择规则不影响文件内完整时间桶。"""
        if i not in chosen and len(chosen)<24:
            chosen.append(i)
    if buckets:
        add(0);add(len(buckets)-1)
        for metric in metric_ids:
            for field,select in [('min',min),('max',max)]:
                candidates=[j for j,(_,b) in enumerate(buckets) if b['metrics'].get(metric,{}).get('distribution',{}).get(field) is not None]
                if candidates:
                    add(select(candidates,key=lambda j:buckets[j][1]['metrics'][metric]['distribution'][field]))
        for i in range(24):
            add(i*(len(buckets)-1)//23)
    result.append(['时间覆盖','代表桶',f'{len(chosen)} / {len(buckets)}；其余桶通过 index/detail 读取'])
    for i in sorted(chosen):
        original,b=buckets[i]
        for metric in metric_ids:
            d=b['metrics'][metric]['distribution']
            result.append(['原时间桶',f'#{original} {b["time"]} / {metric}',
                           {'行数':b['result_row_count'],'P05/P50/P95':[distribution_value(d,k) for k in ('p05','p50','p95')],'max':d['max']}])
    skipped=dimension.get('coverage',{}).get('skipped_pairs',[])
    result.append(['组合覆盖','未计算对数量',len(skipped)])
    return ['类别','对象','描述'],result


def read_profile(statistic_path='examples/sales/output/statistic_profile.json', dimension_path='examples/sales/output/dimension_profile.json',
                 level='overview', dimension=None, values=None, metrics=None, start=None, end=None,
                 offset=0, limit=20, fields=None, max_bytes=8192):
    """基于完整 JSON 的唯一分层读取入口，返回包含 Markdown 表格的字典。

    用法：level=overview/index/detail；dimension=['channel'],values=['ads'] 选择维度组；
    未指定 dimension 时 index/detail 读取时间桶。metrics/fields 限定指标与统计列。
    只读取文件，不执行 SQL、不重算源结果、不持久化会话。最终响应超预算时缩短本页，
    单行仍超限则明确报错。offset/next_offset 是简单页偏移，文件更新后需从头读取。
    """
    if level not in ('overview','index','detail'):
        raise ValueError('level 必须为 overview/index/detail')
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
        raise ValueError('offset 非负，limit 范围 1..100')
    if type(max_bytes) is not int or max_bytes<512:
        raise ValueError('max_bytes 必须至少 512')
    s=json.loads(Path(statistic_path).read_text()); d=json.loads(Path(dimension_path).read_text())
    if s.get('schema_version')!='2.0' or d.get('schema_version')!='1.0':
        raise ValueError('需要完整 StatisticProfile 2.0 和 DimensionProfile 1.0')
    if s['dataset']['result_row_count']!=d['scope']['result_row_count']:
        raise ValueError('两路结果行数不一致')
    if s['scope'].get('dimensions')!=d['scope'].get('dimensions') or s['scope'].get('time')!=d['scope'].get('time'):
        raise ValueError('两路维度/时间口径不一致')
    metric_ids=list(s['metrics']) if metrics is None else metrics
    if not set(metric_ids)<=s['metrics'].keys():
        raise ValueError('未知指标')
    selected_fields=DEFAULT_FIELDS if fields is None else fields
    if not set(selected_fields)<=set(DEFAULT_FIELDS+['sample_count','non_null_count','valid_count','population','quantile_method']):
        raise ValueError('未知统计字段')
    if values is not None and (not dimension or level=='overview'):
        raise ValueError('values 仅用于指定 dimension 的 index/detail')
    if dimension and (start is not None or end is not None):
        raise ValueError('维度组的统计不能再按时间切割；请读取已保存时间桶，或提供该交叉切片的 profile')
    item=locate_dimension(d,dimension) if dimension else None
    groups=choose_groups(item,dimension,values) if item else []
    buckets=time_buckets(s,start,end)
    if level=='overview':
        headers,all_rows=overview(s,d,metric_ids,[item] if item else d['single_dimensions']+d['combinations'],buckets)
    elif level=='index':
        headers=['原数组序号','维度值' if item else '时间桶','结果行数','结果行占比']
        total=s['dataset']['result_row_count']
        all_rows=[[i,g['slice'],g['result_row_count'],g['result_row_share']] for i,g in groups] if item else [
            [i,b['time'],b['result_row_count'],b['result_row_count']/total if total else None] for i,b in buckets]
    else:
        headers=['原数组序号','对象','结果行数','指标']+selected_fields
        source=groups if item else buckets
        all_rows=[[i,g.get('slice',g.get('time')),g['result_row_count'],metric]+[
            distribution_value(g['metrics'][metric]['distribution'],f) for f in selected_fields]
            for i,g in source for metric in metric_ids]
    page=all_rows[offset:offset+limit]
    while True:
        next_offset=offset+len(page)
        result={'level':level,'total_count':len(all_rows),'returned_count':len(page),'has_more':next_offset<len(all_rows),
                'next_offset':next_offset if next_offset<len(all_rows) else None,
                'note':'只描述保存的查询结果；长文本为显示预览，完整信息仍在JSON。分页期间请保持文件不变。',
                'content':table(headers,page)}
        if len((json.dumps(result,ensure_ascii=False,separators=(',',':'))+'\n').encode())<=max_bytes:
            return result
        if len(page)<=1:
            raise ValueError('单条明细超过输出限制；请减少 metrics/fields 或增大 max_bytes')
        page.pop()


def main():
    """CLI：python3 -m profiling.reader --level index --dimension channel。

    --values 为 JSON 数组，--metrics/--fields 为空格分隔列表，返回内容中附分页偏移。
    不创建数据库、会话、签名游标或额外缓存。
    """
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--statistic',default='examples/sales/output/statistic_profile.json')
    parser.add_argument('--dimension-profile',default='examples/sales/output/dimension_profile.json')
    parser.add_argument('--level',choices=['overview','index','detail'],default='overview')
    parser.add_argument('--dimension',nargs='+')
    parser.add_argument('--values',help='JSON 数组，例如 ["ads"] 或 [null]')
    parser.add_argument('--metrics',nargs='+')
    parser.add_argument('--fields',nargs='+')
    parser.add_argument('--start');parser.add_argument('--end')
    parser.add_argument('--offset',type=int,default=0);parser.add_argument('--limit',type=int,default=20)
    parser.add_argument('--max-bytes',type=int,default=8192)
    args=parser.parse_args()
    result=read_profile(args.statistic,args.dimension_profile,args.level,args.dimension,
                        json.loads(args.values) if args.values is not None else None,args.metrics,args.start,args.end,
                        args.offset,args.limit,args.fields,args.max_bytes)
    print(json.dumps(result,ensure_ascii=False,separators=(',',':')))


if __name__=='__main__':
    main()
