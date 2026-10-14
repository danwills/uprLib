import sys

def spinning_cursor():
    # Intended to be called with next( method )
    # TBH I don't really like this design heaps.. 
    # I think I pasted it.. but at least it does
    # mean that no-one has to keep track of an index!?
    while True:
        for cursor in '|/-\\':
            yield cursor
            
def write_spinner(spinner, 
                  prefix=None, 
                  prefix_width=45, 
                  percent=None, 
                  percent_width=5,
                  lines_mode=False):
    """"""
    
    if not spinner:
        return
    
    write_count=0
    
    if prefix:
        #print("prefix is:" + str( prefix ))
        prefix_str = prefix
        if prefix_width:        
             prefix_str = prefix_str.ljust(prefix_width)
        sys.stdout.write( prefix_str )
        sys.stdout.write(" ")
        write_count += len( prefix_str ) + 1
        
    nextchar = next(spinner) if percent != 1.0 else "*"
    spinner_str = "[" + nextchar + "]"
    sys.stdout.write( spinner_str )
    write_count += len( spinner_str )
    
    if percent:
        #print("percent is: " + str(percent))
        percent_str = str(int(percent*100)) + "%"
        if percent_width:
            percent_str = percent_str.rjust(percent_width)
        
        
        sys.stdout.write(percent_str)
        write_count += len(percent_str)
        
    
    if not lines_mode:
        sys.stdout.write('\b'*write_count)
    else :
        sys.stdout.write("\n")
        
    sys.stdout.flush()

def endSpinner(spinner):
    if spinner:
        sys.stdout.write("\n")
    
