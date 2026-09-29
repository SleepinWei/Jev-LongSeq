import pypdfium2 as pdfium
from pathlib import Path
from PIL import Image,ImageDraw
pdf=pdfium.PdfDocument('build/main.pdf')
for i,page in enumerate(pdf):page.render(scale=2.5).to_pil().save(f'preview/slide-{i+1:02d}.png')
files=sorted(Path('preview').glob('slide-*.png'))
for start in range(0,len(files),6):
 out=Image.new('RGB',(1500,1290),'#e3e6e1');d=ImageDraw.Draw(out)
 for i,p in enumerate(files[start:start+6]):
  im=Image.open(p).convert('RGB');im.thumbnail((720,405));x=(i%2)*750;y=(i//2)*430
  out.paste(im,(x+15,y+22));d.text((x+15,y+5),p.stem,fill='black')
 out.save(f'preview/contact-{start//6+1}.png')
print(len(pdf),'pages rendered with PDFium')
