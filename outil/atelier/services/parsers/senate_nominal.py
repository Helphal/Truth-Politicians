"""Read institutional IDs from the official detailed vote list, not fuzzy names."""
from html.parser import HTMLParser
import re
import unicodedata

def normalize(text):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD',text.casefold()) if not unicodedata.combining(c)).split())

class Nominal(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading=None;self.heading_parts=[];self.category=None;self.link=None;self.parts=[];self.rows=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in {'h2','h3','h4'}:self.heading=tag;self.heading_parts=[]
        if tag=='a' and self.category:
            match=re.fullmatch(r'/senateur/[a-z_]+(\d{5}[a-z])\.html',attrs.get('href',''))
            if match:self.link=(match.group(1),attrs['href']);self.parts=[]
    def handle_data(self,data):
        if self.heading:self.heading_parts.append(data)
        if self.link:self.parts.append(data)
    def handle_endtag(self,tag):
        if tag==self.heading:
            text=normalize(' '.join(self.heading_parts))
            self.category={'ont vote pour':'for','ont vote contre':'against','abstentions':'abstention',"n'ont pas pris part au vote":'non_voting'}.get(text)
            self.heading=None
        if tag=='a' and self.link:
            ref,href=self.link;name=' '.join(''.join(self.parts).split())
            self.rows.append({'institutional_ref':ref,'name':name,'category':self.category,'profile_url':'https://www.senat.fr'+href})
            self.link=None;self.parts=[]

def parse_nominal(data):
    if len(data)>10*1024*1024:raise ValueError('senate_document_limit')
    parser=Nominal();parser.feed(data.decode('utf-8-sig'))
    ids=[r['institutional_ref'] for r in parser.rows]
    if not ids:raise ValueError('senate_detailed_list_missing')
    if len(set(ids))!=len(ids):raise ValueError('senate_duplicate_institutional_id')
    return parser.rows
