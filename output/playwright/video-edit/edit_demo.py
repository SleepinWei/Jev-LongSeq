from pathlib import Path
import subprocess, json
from PIL import Image, ImageDraw, ImageFont
ROOT=Path(__file__).resolve().parent
OUT=ROOT.parent
FF='/private/tmp/jev-demo-video-tools/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1'
FONT='/System/Library/Fonts/Hiragino Sans GB.ttc'
def f(n): return ImageFont.truetype(FONT,n)
def card(name,title,lines):
 im=Image.new('RGB',(1600,1100),'#f6f7f2');d=ImageDraw.Draw(im)
 d.rounded_rectangle((110,155,1490,925),radius=28,fill='white',outline='#d7e2d9',width=2)
 d.text((165,220),'Jev LongSeq  /  Trace Studio',font=f(28),fill='#236348')
 d.text((165,340),title,font=f(54),fill='#17261e')
 for n,line in enumerate(lines):d.text((165,450+n*75),line,font=f(29),fill='#56635b')
 im.save(ROOT/name)
def caption(name,title,detail):
 im=Image.new('RGBA',(1600,1100),(0,0,0,0));d=ImageDraw.Draw(im)
 d.rectangle((0,1000,1600,1100),fill='#163f2e')
 d.text((40,1014),title,font=f(28),fill='white')
 d.text((40,1056),detail,font=f(19),fill='#c8ddd0')
 im.save(ROOT/name)
card('title.png','在小红书搜索 OpenAI Aeon 传闻',['Prompt：在小红书上找一下openai aeon 的 rumor','输入任务 → 提交运行 → 回放搜索 → 查看决策与结果','界面实录 · 含明确标注的真实历史回放'])
card('replay-title.png','接下来：真实历史任务回放',['本次新运行遇到网站连接关闭 / Chrome 连接超时。','以下展示 2026-09-27 18:30 的同主题成功记录。','使用系统保存的真实截图、动作与回答。'])
segments=[
 ('title',4,None,0,1,None),
 ('input',7,'tracestudio-openai-aeon-take2.webm',18,1,('01  输入任务','选择“网页任务 · 当前 Chrome”和 DeepSeek API，起始网址留空。')),
 ('launch',8,'tracestudio-openai-aeon-take2.webm',99,1,('02  点击“运行 prompt”','系统按 prompt 选择起始网页，并创建独立任务记录。')),
 ('replay-title',6,None,0,1,None),
 ('replay',42,'tracestudio-openai-aeon-replay.webm',18,1.5,('03  搜索过程与 DOM 标注','历史回放 · 同主题真实记录 · 1.5×：输入 openai aeon，打开搜索结果。')),
 ('decision',6,'tracestudio-openai-aeon-replay.webm',98,1,('04  查看候选动作与模型选择','历史回放 · 拖动时间线返回某一步，检查候选列表及选中的输入控件。')),
 ('trace',4,'tracestudio-openai-aeon-replay.webm',107,1,('05  查看执行轨迹','历史回放 · 搜索、输入、点击、等待与完成状态都保留在轨迹中。')),
 ('answer',11,'tracestudio-openai-aeon-replay.webm',114,1,('06  阅读最终结果','历史回答仅汇总搜索列表信息；未打开笔记正文，传闻细节尚未核实。')),
]
files=[]
for i,(name,duration,source,start,speed,cap) in enumerate(segments):
 target=ROOT/f'{i:02d}-{name}.mp4'
 args=[FF,'-hide_banner','-loglevel','error','-y']
 if source:
  caption(f'{name}-caption.png',*cap)
  args+=['-ss',str(start),'-t',str(duration),'-i',str(OUT/source),'-loop','1','-i',str(ROOT/f'{name}-caption.png')]
  base='crop=1600:650:0:0,pad=1600:1100:0:150:color=0xf6f7f2' if name in ('input','launch') else 'pad=1600:1100:0:0:color=0xf6f7f2'
  args+=['-filter_complex',f'[0:v]setpts=(PTS-STARTPTS)/{speed},{base}[base];[base][1:v]overlay=0:0:shortest=1[v]','-map','[v]','-t',str(duration/speed)]
 else:args+=['-loop','1','-i',str(ROOT/f'{name}.png'),'-t',str(duration)]
 args+=['-an','-r','25','-c:v','libx264','-crf','20','-preset','fast','-pix_fmt','yuv420p',str(target)]
 subprocess.run(args,check=True);files.append(target);print(name,'done',flush=True)
(ROOT/'concat.txt').write_text(''.join(f"file '{p}'\n" for p in files))
final=OUT/'TraceStudio-OpenAI-Aeon-Demo.mp4'
subprocess.run([FF,'-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(ROOT/'concat.txt'),'-c','copy','-movflags','+faststart',str(final)],check=True)
(OUT/'TraceStudio-OpenAI-Aeon-Demo.md').write_text('''# Trace Studio Demo\n\nPrompt：在小红书上找一下openai aeon 的 rumor\n\n视频包含实际输入、提交操作，以及明确标注的同主题历史任务回放。\n本次两次实时尝试分别遇到小红书连接关闭和 Chrome CDP 连接超时。\n历史记录：`runs/ui-prompt-1790505025723049000/task`（2026-09-27 18:30）。\n历史回答仅基于搜索列表；未进入笔记正文，传闻细节未核实。\n\n- 00:00 简介\n- 00:04 输入 prompt\n- 00:11 提交任务\n- 00:19 回放来源说明\n- 00:25 搜索与 DOM 标注（1.5×）\n- 00:53 候选决策\n- 00:59 执行轨迹\n- 01:03 最终结果\n\n视频为 1600×1100，25 fps，74 秒，中文说明字幕，无配音。\n''')
print(final,flush=True)
