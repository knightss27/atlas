import os
from pipeline.modules.master import master

def masterdark(**kwargs) -> cdproc.CCDData:
    """ This function creates a master dark from the 'dark'
    folder of given organized directory. It saves the master
    dark to the directory and returns the created CCDData object. 
    
    Parameters
    -----------

    Returns
    -------
    result: ccdproc.CCDData
        The created master dark
    """
    
    # extract dirname
    dirname = kwargs.get('dirname')

    # check that we have a directory
    if dirname is None:
        return None
    
    # extract dirname
    return master(dirname, 'dark')
