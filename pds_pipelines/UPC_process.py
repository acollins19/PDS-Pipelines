#!/usr/bin/env python
import re
import os
import sys
import datetime
import logging
import hashlib
import json
from ast import literal_eval
import pytz
import pvl

from pysis import isis
from pysis.exceptions import ProcessError
from pysis.isis import getsn

from pds_pipelines.RedisLock import RedisLock
from pds_pipelines.RedisQueue import RedisQueue
from pds_pipelines.Recipe import Recipe
from pds_pipelines.Process import Process
from pds_pipelines.UPCkeywords import UPCkeywords
from pds_pipelines.db import db_connect
from pds_pipelines.models import upc_models, pds_models
from pds_pipelines.models.upc_models import MetaTime, MetaGeometry, MetaString, MetaBoolean
from pds_pipelines.config import pds_log, pds_info, workarea, keyword_def, pds_db, upc_db, lock_obj

from sqlalchemy import and_

def getISISid(infile):
    serial_num = getsn(from_=infile)
    # in later versions of getsn, serial_num is returned as bytes
    if isinstance(serial_num, bytes):
        serial_num = serial_num.decode()
    newisisSerial = serial_num.replace('\n', '')
    return newisisSerial


def find_keyword(obj, key):
    if key in obj:
        return obj[key]
    for _, v in obj.items():
        if isinstance(v, dict):
            F_item = find_keyword(v, key)
            if F_item is not None:
                return F_item

def db2py(key_type, value):
    """ Responsible for coercing database syntax to Python
        syntax (e.g. 'true' to True)

    Parameters
    ----------
    key_type : str
        A string type description of the value.
    value : obj
        The value of the object being coerced to Python syntax

    Returns
    -------
    out : obj
        A Python-syntax value based on the keytype and value pair.
    """

    if key_type == "double":
        if isinstance(value, pvl.Units):
            # pvl.Units(value=x, units=y), we are only interested in value
            value = value.value
        return value
    elif key_type == "boolean":
        return (str(value).lower() == "true")
    else:
        return value


def AddProcessDB(session, fid, outvalue):

    # pdb.set_trace()
    date = datetime.datetime.now(pytz.utc).strftime("%Y-%m-%d %H:%M:%S")

    processDB = pds_models.ProcessRuns(fileid=fid,
                                       process_date=date,
                                       process_typeid='5',
                                       process_out=outvalue)

    try:
        session.merge(processDB)
        session.commit()
        return 'SUCCESS'
    except:
        return 'ERROR'


def get_tid(keyword, session):
    try:
        tid = session.query(upc_models.Keywords.typeid).filter(
            upc_models.Keywords.typename == keyword).first()[0]
        return tid
    except:
        return None


def main():
    # Connect to database - ignore engine information
    pds_session, pds_engine = db_connect(pds_db)

    # Connect to database - ignore engine information
    session, upc_engine = db_connect(upc_db)

    # ***************** Set up logging *****************
    logger = logging.getLogger('UPC_Process')
    logger.setLevel(logging.INFO)
    logFileHandle = logging.FileHandler(pds_log + 'Process.log')
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s, %(message)s')
    logFileHandle.setFormatter(formatter)
    logger.addHandler(logFileHandle)

    PDSinfoDICT = json.load(open(pds_info, 'r'))

    # Redis Queue Objects
    RQ_main = RedisQueue('UPC_ReadyQueue')
    logger.info("UPC Processing Queue: %s", RQ_main.id_name)
    RQ_lock = RedisLock(lock_obj)
    # If the queue isn't registered, add it and set it to "running"
    RQ_lock.add({RQ_main.id_name: '1'})

    proc_date_tid = get_tid('processdate', session)
    err_type_tid = get_tid('errortype', session)
    err_msg_tid = get_tid('errormessage', session)
    err_flag_tid = get_tid('error', session)
    isis_footprint_tid = get_tid('isisfootprint', session)
    isis_centroid_tid = get_tid('isiscentroid', session)
    start_time_tid = get_tid('starttime', session)
    stop_time_tid = get_tid('stoptime', session)
    checksum_tid = get_tid('checksum', session)

    # while there are items in the redis queue
    while int(RQ_main.QueueSize()) > 0 and RQ_lock.available(RQ_main.id_name):
        # get a file from the queue
        item = literal_eval(RQ_main.QueueGet().decode("utf-8"))
        inputfile = item[0]
        fid = item[1]
        archive = item[2]
        #inputfile = (RQ_main.QueueGet()).decode('utf-8')
        if os.path.isfile(inputfile):
            pass
        else:
            print("{} is not a file\n".format(inputfile))
        if os.path.isfile(inputfile):
            logger.info('Starting Process: %s', inputfile)

            # @TODO refactor this logic.  We're using an object to find a path, returning it,
            #  then passing it back to the object so that the object can use it.
            recipeOBJ = Recipe()
            recipe_json = recipeOBJ.getRecipeJSON(archive)
            #recipe_json = recipeOBJ.getRecipeJSON(getMission(str(inputfile)))
            recipeOBJ.AddJsonFile(recipe_json, 'upc')

            infile = workarea + os.path.splitext(
                str(os.path.basename(inputfile)))[0] + '.UPCinput.cub'
            outfile = workarea + os.path.splitext(
                str(os.path.basename(inputfile)))[0] + '.UPCoutput.cub'
            caminfoOUT = workarea + os.path.splitext(
                str(os.path.basename(inputfile)))[0] + '_caminfo.pvl'
            EDRsource = inputfile.replace(
                '/pds_san/PDS_Archive/',
                'https://pdsimage.wr.ugs.gov/Missions/')

            status = 'success'
            # Iterate through each process listed in the recipe
            for item in recipeOBJ.getProcesses():
                # If any of the processes failed, discontinue processing
                if status.lower() == 'error':
                    break
                elif status.lower() == 'success':
                    processOBJ = Process()
                    processOBJ.ProcessFromRecipe(item, recipeOBJ.getRecipe())
                    # Handle processing based on string description.
                    if '2isis' in item:
                        processOBJ.updateParameter('from_', inputfile)
                        processOBJ.updateParameter('to', outfile)
                    elif item == 'thmproc':
                        processOBJ.updateParameter('from_', inputfile)
                        processOBJ.updateParameter('to', outfile)
                        thmproc_odd = str(workarea) + str(os.path.splitext(
                            os.path.basename(inputfile))[0]) + '.UPCoutput.raw.odd.cub'
                        thmproc_even = str(workarea) + str(
                            os.path.splitext(os.path.basename(
                                inputfile))[0]) + '.UPCoutput.raw.even.cub'
                    elif item == 'handmos':
                        processOBJ.updateParameter('from_', thmproc_even)
                        processOBJ.updateParameter('mosaic', thmproc_odd)
                    elif item == 'spiceinit':
                        processOBJ.updateParameter('from_', infile)
                    elif item == 'cubeatt':
                        band_infile = infile + '+' + str(1)
                        processOBJ.updateParameter('from_', band_infile)
                        processOBJ.updateParameter('to', outfile)
                    elif item == 'footprintinit':
                        processOBJ.updateParameter('from_', infile)
                    elif item == 'caminfo':
                        processOBJ.updateParameter('from_', infile)
                        processOBJ.updateParameter('to', caminfoOUT)
                    else:
                        processOBJ.updateParameter('from_', infile)
                        processOBJ.updateParameter('to', outfile)

                    pwd = os.getcwd()
                    # iterate through functions listed in process obj
                    for k, v in processOBJ.getProcess().items():
                        # load a function into func
                        func = getattr(isis, k)
                        try:
                            os.chdir(workarea)
                            # execute function
                            func(**v)
                            os.chdir(pwd)
                            if item == 'handmos':
                                if os.path.isfile(thmproc_odd):
                                    os.rename(thmproc_odd, infile)
                            else:
                                if os.path.isfile(outfile):
                                    os.rename(outfile, infile)
                            status = 'success'
                            if '2isis' in item:
                                label = pvl.load(infile)
                                infile_bandlist = label['IsisCube']['BandBin'][PDSinfoDICT[archive]['bandbinQuery']]
                                infile_centerlist = label['IsisCube']['BandBin']['Center']
                            elif item == 'thmproc':
                                pass
                            elif item == 'handmos':
                                label = pvl.load(infile)
                                infile_bandlist = label['IsisCube']['BandBin'][PDSinfoDICT[archive]['bandbinQuery']]
                                infile_centerlist = label['IsisCube']['BandBin']['Center']

                        except ProcessError as e:
                            print(e)
                            status = 'error'
                            processError = item

            # keyword definitions
            keywordsOBJ = None
            if status.lower() == 'success':
                try:
                    keywordsOBJ = UPCkeywords(caminfoOUT)
                except:
                    with open(caminfoOUT, 'r') as f:
                        filedata = f.read()

                    filedata = filedata.replace(';', '-').replace('&', '-')
                    filedata = re.sub(r'\-\s+', r'', filedata, flags=re.M)

                    with open(caminfoOUT, 'w') as f:
                        f.write(filedata)

                    keywordsOBJ = UPCkeywords(caminfoOUT)
                target_Qobj = session.query(upc_models.Targets).filter(
                    upc_models.Targets.targetname == keywordsOBJ.getKeyword(
                        'TargetName').upper()).first()

                instrument_Qobj = session.query(upc_models.Instruments).filter(
                    upc_models.Instruments.instrument == keywordsOBJ.getKeyword(
                        'InstrumentId')).first()

                if session.query(upc_models.DataFiles).filter(
                        upc_models.DataFiles.isisid == keywordsOBJ.getKeyword(
                            'IsisId')).first() is None:

                    test_input = upc_models.DataFiles(
                        isisid=keywordsOBJ.getKeyword('IsisId'),
                        productid=keywordsOBJ.getKeyword('ProductId'),
                        edr_source=EDRsource,
                        edr_detached_label='',
                        instrumentid=instrument_Qobj.instrumentid,
                        targetid=target_Qobj.targetid)

                    session.merge(test_input)
                    session.commit()

                Qobj = session.query(upc_models.DataFiles).filter(
                    upc_models.DataFiles.isisid == keywordsOBJ.getKeyword('IsisId')).first()

                UPCid = Qobj.upcid
                print(UPCid)
                # block to add band information to meta_bands
                if isinstance(infile_bandlist, list):
                    index = 0
                    while index < len(infile_bandlist):
                        B_DBinput = upc_models.MetaBands(
                            upcid=UPCid, filter=str(
                                infile_bandlist[index]), centerwave=infile_centerlist[index])
                        session.merge(B_DBinput)
                        index = index + 1
                else:
                    try:
                        # If infile_centerlist is in "Units" format, grab the value
                        f_centerlist = float(infile_centerlist[0])
                    except TypeError:
                        f_centerlist = float(infile_centerlist)
                    B_DBinput = upc_models.MetaBands(upcid=UPCid, filter=infile_bandlist, centerwave=f_centerlist)
                    session.merge(B_DBinput)
                session.commit()

                # Block to add common keywords
                testjson = json.load(
                    open(keyword_def, 'r'))
                for element_1 in testjson['instrument']['COMMON']:
                    keyvalue = ""
                    keytype = testjson['instrument']['COMMON'][element_1]['type']
                    keyword = testjson['instrument']['COMMON'][element_1]['keyword']
                    keyword_Qobj = session.query(upc_models.Keywords).filter(
                        and_(upc_models.Keywords.typename == element_1,
                             upc_models.Keywords.instrumentid == 1)).first()

                    if keyword_Qobj is None:
                        continue
                    else:
                        keyvalue = keywordsOBJ.getKeyword(keyword)
                    if keyvalue is None:
                        continue
                    keyvalue = db2py(keytype, keyvalue)
                    try:
                        DBinput = upc_models.create_table(keytype,
                                                          upcid=UPCid,
                                                          typeid=keyword_Qobj.typeid,
                                                          value=keyvalue)
                    except Exception as e:
                        logger.warn("Unable to enter %s into table\n\n%s", keytype, e)
                        continue
                    session.merge(DBinput)
                    try:
                        session.flush()
                    except:
                        logger.warn("Unable to flush database connection")
                session.commit()

                for element_1 in testjson['instrument'][archive]:
                    keyvalue = ""
                    keytype = testjson['instrument'][archive][element_1]['type']
                    keyword = testjson['instrument'][archive][element_1]['keyword']
                    keyword_Qobj = session.query(upc_models.Keywords).filter(
                        and_(upc_models.Keywords.typename == element_1,
                             upc_models.Keywords.instrumentid.in_(
                                 (1, instrument_Qobj.instrumentid)))).first()

                    if keyword_Qobj is None:
                        continue
                    else:
                        keyvalue = keywordsOBJ.getKeyword(keyword)
                    if keyvalue is None:
                        logger.debug("Keyword %s not found", keyword)
                        continue
                    keyvalue = db2py(keytype, keyvalue)
                    try:
                        DBinput = upc_models.create_table(keytype,
                                                          upcid=UPCid,
                                                          typeid=keyword_Qobj.typeid,
                                                          value=keyvalue)
                    except Exception as e:
                        logger.warn("Unable to enter %s into database\n\n%s", keytype, e)
                        continue
                    session.merge(DBinput)
                    try:
                        session.flush()
                    except:
                        logger.warn("Unable to flush database connection")
                session.commit()

                # geometry stuff
                G_centroid = 'point ({} {})'.format(
                    str(keywordsOBJ.getKeyword('CentroidLongitude')),
                    str(keywordsOBJ.getKeyword('CentroidLatitude')))

                G_keyword_Qobj = session.query(upc_models.Keywords.typeid).filter(
                    upc_models.Keywords.typename == 'isiscentroid').first()
                G_footprint_Qobj = session.query(upc_models.Keywords.typeid).filter(
                    upc_models.Keywords.typename == 'isisfootprint').first()
                G_footprint = keywordsOBJ.getKeyword('GisFootprint')
                G_DBinput = upc_models.MetaGeometry(upcid=UPCid,
                                                    typeid=G_keyword_Qobj,
                                                    value=G_centroid)
                session.merge(G_DBinput)
                G_DBinput = upc_models.MetaGeometry(upcid=UPCid,
                                                    typeid=G_footprint_Qobj,
                                                    value=G_footprint)
                session.merge(G_DBinput)
                session.flush()
                session.commit()

                f_hash = hashlib.md5()
                with open(inputfile, "rb") as f:
                    for chunk in iter(lambda: f.read(4096), b""):
                        f_hash.update(chunk)
                checksum = f_hash.hexdigest()


                DBinput = upc_models.MetaString(upcid=UPCid, typeid=checksum_tid, value=checksum)
                session.merge(DBinput)
                DBinput = upc_models.MetaBoolean(upcid=UPCid, typeid=err_flag_tid, value=False)
                session.merge(DBinput)
                session.commit()
                AddProcessDB(pds_session, fid, True)
                os.remove(infile)
                os.remove(caminfoOUT)

            elif status.lower() == 'error':
                try:
                    label = pvl.load(infile)
                except Exception as e:
                    logger.info('%s', e)
                    continue
                date = datetime.datetime.now(pytz.utc).strftime(
                    "%Y-%m-%d %H:%M:%S")

                if '2isis' in processError or processError == 'thmproc':
                    if session.query(upc_models.DataFiles).filter(
                            upc_models.DataFiles.edr_source == EDRsource.decode(
                                "utf-8")).first() is None:

                        target_Qobj = session.query(upc_models.Targets).filter(
                            upc_models.Targets.targetname == str(
                                label['IsisCube']['Instrument']['TargetName']).upper()).first()

                        instrument_Qobj = session.query(upc_models.Instruments).filter(
                            upc_models.Instruments.instrument == str(
                                label['IsisCube']
                                ['Instrument']
                                ['InstrumentId'])).first()

                        error1_input = upc_models.DataFiles(isisid='1',
                                                            edr_source=EDRsource)
                        session.merge(error1_input)
                        session.commit()

                    EQ1obj = session.query(upc_models.DataFiles).filter(
                        upc_models.DataFiles.edr_source == EDRsource).first()
                    UPCid = EQ1obj.upcid

                    errorMSG = 'Error running {} on file {}'.format(
                        processError, inputfile)

                    DBinput = MetaTime(upcid=UPCid,
                                       typeid=proc_date_tid,
                                       value=date)
                    session.merge(DBinput)

                    DBinput = MetaString(upcid=UPCid,
                                         typeid=err_type_tid,
                                         value=processError)
                    session.merge(DBinput)

                    DBinput = MetaString(upcid=UPCid,
                                         typeid=err_msg_tid,
                                         value=errorMSG)
                    session.merge(DBinput)

                    DBinput = MetaBoolean(upcid=UPCid,
                                          typeid=err_flag_tid,
                                          value=True)
                    session.merge(DBinput)

                    DBinput = MetaGeometry(upcid=UPCid,
                                           typeid=isis_footprint_tid,
                                           value='POINT(361 0)')
                    session.merge(DBinput)

                    DBinput = MetaGeometry(upcid=UPCid,
                                           typeid=isis_centroid_tid,
                                           value='POINT(361 0)')
                    session.merge(DBinput)

                    session.commit()
                else:
                    try:
                        label = pvl.load(infile)
                    except Exception as e:
                        logger.warn('%s', e)
                        continue

                    isisSerial = getISISid(infile)

                    if session.query(upc_models.DataFiles).filter(
                            upc_models.DataFiles.isisid == isisSerial).first() is None:
                        target_Qobj = session.query(upc_models.Targets).filter(
                            upc_models.Targets.targetname == str(
                                label['IsisCube']['Instrument']['TargetName'])
                            .upper()).first()
                        instrument_Qobj = session.query(upc_models.Instruments).filter(
                            upc_models.Instruments.instrument == str(
                                label['IsisCube']
                                ['Instrument']
                                ['InstrumentId'])).first()

                        if target_Qobj is None or instrument_Qobj is None:
                            continue

                        error2_input = upc_models.DataFiles(isisid=isisSerial, productid=label['IsisCube']['Archive']['ProductId'], edr_source=EDRsource, instrumentid=instrument_Qobj.instrumentid, targetid=target_Qobj.targetid)
                    session.merge(error2_input)
                    session.commit()

                    try:
                        EQ2obj = session.query(upc_models.DataFiles).filter(
                            upc_models.DataFiles.isisid == isisSerial).first()
                        UPCid = EQ2obj.upcid
                        errorMSG = 'Error running {} on file {}'.format(
                            processError, inputfile)

                        DBinput = MetaTime(upcid=UPCid,
                                           typeid=proc_date_tid,
                                           value=date)
                        session.merge(DBinput)

                        DBinput = MetaString(upcid=UPCid,
                                             typeid=err_type_tid,
                                             value=processError)
                        session.merge(DBinput)

                        DBinput = MetaString(upcid=UPCid,
                                             typeid=err_msg_tid,
                                             value=errorMSG)
                        session.merge(DBinput)

                        DBinput = MetaBoolean(upcid=UPCid,
                                              typeid=err_flag_tid,
                                              value=True)
                        session.merge(DBinput)

                        DBinput = MetaGeometry(upcid=UPCid,
                                               typeid=isis_footprint_tid,
                                               value='POINT(361 0)')
                        session.merge(DBinput)

                        DBinput = MetaGeometry(upcid=UPCid,
                                               typeid=isis_centroid_tid,
                                               value='POINT(361 0)')
                        session.merge(DBinput)
                    except:
                        pass

                    try:
                        v = label['IsisCube']['Instrument']['StartTime']
                    except KeyError:
                        v = None
                    except:
                        continue

                    try:
                        DBinput = MetaTime(upcid=UPCid,
                                           typeid=start_time_tid,
                                           value=v)
                        session.merge(DBinput)
                    except:
                        continue

                    try:
                        v = label['IsisCube']['Instrument']['StopTime']
                    except KeyError:
                        v = None
                    DBinput = MetaTime(upcid=UPCid,
                                       typeid=stop_time_tid,
                                       value=v)
                    session.merge(DBinput)

                    session.commit()

                AddProcessDB(pds_session, fid, False)
                os.remove(infile)

    # Disconnect from db sessions
    pds_session.close()
    session.close()
    # Disconnect from the engines
    pds_engine.dispose()
    upc_engine.dispose()
    logger.info("UPC processing exited successfully")

if __name__ == "__main__":
    sys.exit(main())
