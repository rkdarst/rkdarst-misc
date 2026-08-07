import argparse
import re
import sys
import xml.etree.ElementTree as ET

gpxns = 'http://www.topografix.com/GPX/1/1'
osmandns = 'osmand'

def any_in(lst, item):
    for val in lst:
        if val in item: return True
    return False

def main():
    new = None
    newroot = None

    for input_n, input in enumerate(args.input):
        et = ET.parse(input)
        if new is None:
            new = et
            newroot  = et.getroot()
        root = et.getroot()
        
        ns = {'gpx': gpxns}
        
        for wpt in et.findall('.//gpx:wpt', ns):
            e_type = wpt.find('gpx:type', ns)
            if e_type is not None:
                # Remove uninteresting points
                if args.wikivoyage and any_in(['sleep', 'drink'], e_type.text.lower()):
                    root.remove(wpt)
                    continue
                    
                
    
                e_name = wpt.find('gpx:name', ns)
                if args.wikivoyage and (m := re.match(r'\d+\-(.*)', e_type.text)):
                    type = m.group(1)
                else:
                    type = e_type.text

                if (extensions := wpt.find('gpx:extensions', ns)) is not None:
                    pass
                else:
                    extensions = ET.SubElement(wpt, f'{{{gpxns}}}extensions')
                    
                # icons don't currently work
                if 'eat'   in type: icon = ET.SubElement(extensions, f'{{{osmandns}}}icon') ;  icon.text = 'restaurants'
                elif 'see' in type: icon = ET.SubElement(extensions, f'{{{osmandns}}}icon') ;  icon.text = 'camera'
                elif 'do'  in type: icon = ET.SubElement(extensions, f'{{{osmandns}}}icon') ;  icon.text = 'special_hiking'
                elif 'buy' in type: icon = ET.SubElement(extensions, f'{{{osmandns}}}icon') ;  icon.text = 'shop'
                elif 'go'  in type: icon = ET.SubElement(extensions, f'{{{osmandns}}}icon') ;  icon.text = 'railway_tram_stop'
                else:               icon = ET.SubElement(extensions, f'{{{osmandns}}}icon') ;  icon.text = 'wiki_place'
                bg = ET.SubElement(extensions, f'{{{osmandns}}}background') ;  bg.text = 'octagon'

                #e_name.text = f'{type} {e_name.text}'
                e_name.text = f'{e_name.text}'
                wpt.remove(e_type)

            if input_n != 0:
                newroot.insert(-1, wpt)

    new.write(sys.stdout, encoding="unicode", xml_declaration=True)

parser = argparse.ArgumentParser()
parser.add_argument('input', nargs='+')
parser.add_argument('--wikivoyage', action='store_true')
args = parser.parse_args()
main()
