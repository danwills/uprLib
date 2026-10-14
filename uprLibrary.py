#!/usr/bin/env python

import os
import re
import zlib
import glob
import argparse
import logspinner

def configLinesToDict(lines):
    retdict = {}
    for line in lines:
        # trim out comments safely somehow here..
        # Just going with line-starts-with-# for the moment..
        # just want a way to leave notes.. not meant to be a complex format!
        if line.strip().startswith("#"):
            continue
        lspl = line.split("=")
        if len(lspl) < 2:
            continue
        if lspl[0].strip() in retdict:
            print("Collision! " + lspl[0].strip() + " is already in the dict! overwriting..")
        # TODO: Actually parse strings instead of just stripping the quotes off!!!!
        # Note, support for commas is there for specific key: "upr_active_files" .. 
        # might become generic eventually? we'll see..
        retdict[lspl[0].strip()] = lspl[1].strip().strip('"')
    return retdict

numLayersRgx = re.compile(r'\blayers\s*=\s*(\d+)')

def extractNumLayers(upr_text):
    match = numLayersRgx.search(upr_text)
    return int(match.group(1)) if match else None

class UPRFile:

    def __init__(self, path, verbose=0, num_upr_limit=None):
        self.path = path
        self.verbose = verbose
        self.lines = self.readFileLines()
        self.num_upr_limit = num_upr_limit
        self.uprMetaDict = {}
        self.uprEntryDict = self.makeUPREntryDict()

    def readFileLines(self):
        if not os.path.isfile(self.path):
            print("Error: Could not open file at: " + self.path())
        if self.verbose > 2 :
            print("Opening upr file for reading: " + self.path)

        #fope = open(self.path, "r", encoding="utf-8")
        fope = open(self.path, "r", encoding="ISO-8859-1")

        return fope.readlines()

    def makeUPREntryDict( self, decode_and_unzip=True ):
        upr_entry_dict = {}
        # Thanks to someone on stackOverflow for this wonderful regex incantation:
        # multi-line curly braces, non-greedy and groups the fractal name too
        rgx = r'([\w-]+)\s*[^{]*{\s*([^}]+)\s*}'
        count = 0
        # print("Parseing blocks..")
        # fnames, fpayloads = re.findall( rgx, "".join( self.lines ) )
        # print("Building dict (and decompressing compressed UPRs)..")
        # TODO: spinner or progress bar here
        for fractal_name, fractal_payload in re.findall(rgx, "".join(self.lines)):  # fnames, fpayloads :
            # Stop once we have a specified number of them.
            if self.num_upr_limit and count+1 > self.num_upr_limit:
               break
            
            frackey = fractal_name + ":" + str(count)

            if frackey in upr_entry_dict:
                # This should be impossible now!
                print("COLLISION! we already have something called: " + str(frackey))
                continue
            else:
                if fractal_payload.startswith("::") and decode_and_unzip :
                    if self.verbose > 4 :
                        print("Storing UPR after decoding: " + frackey)
                    # Then it's compressed.. gotta decompress!
                    encoded_str = "".join(fractal_payload.strip(":").strip().split())
                    encoded_bytes = encoded_str.encode()
                    fractal_payload_decoded = str(decodeAndUnzipUPR(encoded_bytes))
                    
                    # Gotta sort out those pesky newlines! (windows?)
                    # Didn't bloody work anyway.. splitlnes seems to tho!
                    #fpd_linefix = fractal_payload_decoded.replace(r"\r\n", "\n")
                    
                    fpd_linefix = "\n".join(l for l in fractal_payload_decoded.splitlines())
                    # upr_obj = UPR( fractal_name, count
                    upr_entry_dict[frackey] = fpd_linefix
                    #if self.verbose:
                        #print("[ " + fractal_name + " ] (compressed)")

                    # Compressed size here is the size (in bytes) of the zlib+CRC
                    # blob prior to base64-style encoding, i.e. 3 bytes per 4
                    # encoded characters - not the on-disk (base64) size.
                    self.uprMetaDict[frackey] = {
                        'num_layers': extractNumLayers(fpd_linefix),
                        'compressed_size': (len(encoded_bytes) // 4) * 3,
                    }
                else:
                    if self.verbose > 4:
                        print("Storing UPR (was already not encoded):" + frackey)
                    upr_entry_dict[frackey] = fractal_payload
                    #if self.verbose:
                        #print("[ " + fractal_name + " ] (uncompressed)")

                    self.uprMetaDict[frackey] = {
                        'num_layers': extractNumLayers(fractal_payload),
                        'compressed_size': len(fractal_payload),
                    }
                count += 1
        return upr_entry_dict

    def getUPRDict(self) :
        return self.uprEntryDict

    def getUPRMetaDict(self) :
        return self.uprMetaDict

class UPR:
    def __init__(self, name, index, uprtext):
        self.name = name
        self.index = index
        self.uprtext = uprtext
        
class UPRLibrary:

    def __init__(self, 
                 num_upr_file_limit=None, 
                 num_upr_limit=None, 
                 config_filename=None, 
                 shell_ui=False, 
                 verbose=False):
                  
        if config_filename:
            self.config_filename = config_filename
        else:
            self.config_filename = "uprLibrary.config"
        self.upr_root_dir = None
        self.upr_current_file = None
        self.upr_active_files = None # List later
        self.config_dict = {}
        self.verbose = verbose
        self.readConfig()
        self.num_upr_file_limit = num_upr_file_limit
        self.num_upr_limit = num_upr_limit
        self.upr_files = []
        self.shell_ui = shell_ui

    def getConfigFileLines(self):
        if self.verbose:
            print("Reading config file: " + self.config_filename)
            
        config_file = open(self.config_filename)
        if not config_file:
            raise Exception("Config file could not be found: " + self.config_filename)
        return config_file.readlines()

    def readConfig(self):
        config_lines = self.getConfigFileLines()
        if not config_lines:
            return
        if self.verbose:
            print("Got: " + str(len(config_lines)) + " config file lines.")
        self.config_dict.update(configLinesToDict(config_lines))
        self.configDictToVars()

    def getConfigDict(self):
        # Gets read in the constructor so should be ok to return like this!
        return self.config_dict

    def configDictToVars(self):
        # Note secret population of locals dict here! not great!
        # comment the hell out of it I guess, make it searchable!
        for cdkey, cdval in self.config_dict.items():
            if hasattr(self, cdkey):
                if cdkey == "upr_active_files" :
                    setattr(self, cdkey, cdval.split(","))
                else :
                    setattr(self, cdkey, cdval)
        if self.verbose > 1:
            print("Got lines from config dict: " + str(self.config_dict))

    def getCurrentUpr(self):
        # print("Current UPR: " + str( self.upr_current_file ) )
        for uprfile in self.upr_files:
            if uprfile.path == os.path.join( self.upr_root_dir, self.upr_current_file ) :
                return uprfile
        return None

    def loadAllUpr(self):
        
        spinner = logspinner.spinning_cursor() if self.shell_ui else None
        
        logspinner.write_spinner(spinner, 
                                 prefix="Globbing UPRs", 
                                 percent=0.0)
                                     
        uprglob = os.path.join( self.upr_root_dir, "*.upr" )
        uprglobmatches = glob.glob( uprglob )
        active_uprs = [ upr for upr in uprglobmatches
                        if os.path.basename( upr ) in self.upr_active_files ]
        
        logspinner.write_spinner(spinner, 
                                 prefix="Globbing UPRs", 
                                 percent=1.0)
        
        logspinner.endSpinner(spinner)
        
        for ix,active_upr in enumerate( active_uprs ) :
            # When option not specified, don't limit!
            if self.num_upr_file_limit and ix+1 > self.num_upr_file_limit :
                break
                
            # TODO: Don't divide by zero!
            
            percent = ix/(max(1,len(active_uprs) - 1))
            logspinner.write_spinner(spinner, 
                                 prefix="Opening UPRs", 
                                 percent=percent)
                             
            self.upr_files.append( UPRFile(active_upr, 
                                           num_upr_limit=self.num_upr_limit,
                                           verbose=self.verbose) )
        logspinner.endSpinner(spinner)
        # print( "\n".join( active_uprs ) )

    # Dict of file-path to UprFile-dict at present!
    def getAllUPRs(self):
        all_uprs = {}
        for upr_file in self.upr_files :
            all_uprs[ upr_file.path ] = upr_file.getUPRDict()
        return all_uprs

    # Dict of file-path to { frackey: {'num_layers':.., 'compressed_size':..} }
    def getAllUPRMeta(self):
        all_meta = {}
        for upr_file in self.upr_files :
            all_meta[ upr_file.path ] = upr_file.getUPRMetaDict()
        return all_meta

base64alphabet = b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'


# this is a bloody crap way of doing this.. but whatever
def getIndex(char):
    return base64alphabet.find(char)

# This takes a string of length at least 4!
# There is no exception handling if something else gets in!
def quadDecode(four_chars):
    # print( "fourChars is: " + str( fourChars ) )

    i0 = getIndex(four_chars[0])
    i1 = getIndex(four_chars[1])
    i2 = getIndex(four_chars[2])
    i3 = getIndex(four_chars[3])

    # print( "i0 is: " + str( i0 ) )
    csum = (i3 << 18) + (i2 << 12) + (i1 << 6) + i0

    byte0 = csum & 0xff
    byte1 = (csum >> 8) & 0xff
    byte2 = (csum >> 16) & 0xff
    # print( "[ byte0, byte1, byte2 ] is: " + str( [ byte0, byte1, byte2 ] ) )
    return [byte0, byte1, byte2]


# Argument instr needs to be bytes, so .encode()!!
def decodeAndUnzipUPR( instr ):
    # instr = "".join( instr.split() ).strip()
    # print( "instr: " + "".join( instr.split() ).strip() )

    # Detect an encoded/compressed chunk (first two characters are colon '::')
    # un-interleave the bytes, as per Fred's advice, then decode, then unzip.
    
    # Fred Says: 
    # They are base64-encoded, but I made an error here and reversed the first 
    # and third bytes of each triplet (if I remember correctly). 
    # So any off-the-shelf base64 code won't work. 
    # After decoding, the entry starts with a 4-byte CRC value, and then 
    # zlib-compressed UPR data.
    # My Notes:
    # by 'reversed' Fred means 'swapped'.
    # Guess this will probably only matter if one day we ever want to /write/ upr?
    # I can't see the swap below at the moment.. maybe just blind?
    decoded = []
    for i in range(0, int(len(instr) / 4)):
        fourchars = instr[i * 4: i * 4 + 4]
        decoded.extend(quadDecode(fourchars))
    # print( "decoded is: " + str( decoded ) )

    decodedchr = [bytes([x]) for x in decoded[4:]]
    # print( "decodedchr is: " + str( decodedchr ) )

    dcdstr = (b"".join(decodedchr))
    # print( decodedchr )
    # return zlib.decompress( decodedchr )
    # to (de-)compress deflate format, use wbits = -zlib.MAX_WBITS
    # to (de-)compress zlib format, use wbits = zlib.MAX_WBITS
    # to (de-)compress gzip format, use wbits = zlib.MAX_WBITS | 16
    # return zlib.decompress( dcdstr, wbits=-zlib.MAX_WBITS )
    return zlib.decompress(dcdstr).decode("utf-8")

# TEST main, DELETE soon.. see uprLib.py for the app/ui/cli
if __name__ == "__main__":
    
    # NOT USED AT PRESENT!
    parser=argparse.ArgumentParser()
    parser.add_argument("-v", "--verbosity", help="Increase output verbosity.", action="store_true")
    args=parser.parse_args()
    
    if args.verbosity:
        print("Verbose mode enabled")
    
    upr_lib = UPRLibrary()
    upr_lib.loadAllUpr()
    curr_upr = upr_lib.getCurrentUpr()
    print("Current UPR: " + curr_upr.path )
    #upr_file = UPRFile(curr_upr)
    #print("This UPR file has: " + str(len(upr_file.lines)) + " lines.")
    upr_dict = curr_upr.getUPRDict()
    print("Loaded: " + str(len(upr_dict)) + " fractals from 'current' UPR.")
    #for fkey in sorted( upr_dict.keys() ) :
    #    print( fkey )
    #print(upr_file.uprEntryDict[list(upr_file.uprEntryDict.keys())[0]])



