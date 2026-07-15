from datetime import datetime
import os
import time
import csv
import shutil
import pytz
from csv import DictWriter
from . import config_default as config

#%%

def datetime2str(date:datetime):
    return date.strftime(config.date_format)

def str2datetime(date_str):
    return datetime.strptime(date_str, config.date_format)
    

class File_Logger():
    
    def __init__(self, file_path_structure, config, debug = False):
        self.config = config
        self.file_path_structure = file_path_structure
        self.old_data = None
        self.cached_data = None
        self.old_date_str = 'NaT'
        self.timezone = pytz.timezone(config.tz)
        self.debug = debug
        self.initialized = False
        
    def get_output_file_path(self, t_now):
        date_str =  t_now.strftime(self.config.date_format)
        log_filepath = self.file_path_structure.format(date_str=date_str)
        
        return log_filepath
        
    def update_existing_file(self, 
                             t_now,
                             data):
        
        if 'time' not in data.keys():
            fieldnames = (
                ["time"] + list(data.keys())
                )
        else:
            fieldnames = list(data.keys())
        
        self.fieldnames = fieldnames
    
        # now = datetime.now(tz=timezone) # current date and time
        file_filepath = self.get_output_file_path(t_now)
        date_str = t_now.strftime(self.config.date_format)
    
        # date_str = pd.Timestamp.now().strftime(config.date_format)
    
        if not os.path.exists(file_filepath):
            self.old_date_str =  None
            self.fieldnames = fieldnames
            self.initialized = True
        else:
    
            tt = time.time()
            print("Loading from disk and extending with new columns..", end="")
            # df = pd.read_csv(filename, index_col=0)
            reader = csv.DictReader(open(file_filepath))
            columns = reader.fieldnames
            # update file if new columns or new order
        
            # header is only updated if more fieldnames are not all in existing columns
            update_header = not set(fieldnames).issubset(set(columns))
        
            if update_header:
                fieldnames = set(fieldnames).union(set(columns)) - set(['time'])
                fieldnames = ['time'] + list(fieldnames)
                shutil.move(file_filepath, file_filepath + '_previous_data')
                reader = csv.DictReader(open(file_filepath + '_previous_data'))
                with open(file_filepath, mode="a") as f:
                    writer = DictWriter(f, fieldnames)
                    writer.writeheader()
                    for row in reader:
                        writer.writerow(row)
                self.fieldnames = fieldnames
            else:
                self.fieldnames = columns
                
            if self.debug:
                print(f".done in {time.time() - tt:2.2f}s")

            self.old_date_str =  date_str
            self.initialized = True
        
    
    def _write_headers(self, filename, fieldnames):
        with open(filename, mode="a") as fid:
            writer = DictWriter(fid, fieldnames)
    
            if self.debug:
                print(f"Writing head for new file {filename}")
            writer.writeheader()
    
    def _write_data(self, filename, row_data, is_new_day):
        if row_data is not None:
            with open(filename, mode="a") as fid:
                
    
                if is_new_day:
                    # new file was started we need to output the header
                    self._write_headers(filename, self.fieldnames)
                
                # row.update(data)
                writer = DictWriter(fid, self.fieldnames)
                writer.writerow(row_data)
                
    def log_step(self, t_now, data):
        
        if not self.initialized:
            
            self.update_existing_file(t_now, data)
            
        if 'time' not in data.keys():
            
            now_str = t_now.strftime("%H:%M:%S")
            row_data = dict(time=now_str)
            row_data.update(data)
        else:
            row_data = data
            
        filepath = self.get_output_file_path(t_now)
        #flag if data dict is different from old 
        data_changed = (data != self.old_data)
        
        date_str = t_now.strftime("%y-%m-%d")
        is_new_day  = (self.old_date_str != date_str)
        
        if self.config.logger_skip_no_changes and (data_changed or is_new_day):
            
            if self.cached_data is not None:
                print("Data changed in timestep {now_str} - writing out cached data for {cached_data['meas'][data']['time']")
                self._write_data(**self.cached_data)
                
            if self.debug:
                print(f"Writing data for  {row_data['time']}")    
            
            self._write_data(filepath, row_data, is_new_day)
           
            self.cached_data = None
        else:
            
            print(f"Data for {now_str} is identical - not writing data data, caching data row.")
            self.cached_data = dict(filename=filepath, 
                                row_data = row_data.copy(),
                                is_new_day = is_new_day)
            row_data = None
            
        self.filepath = filepath
        self.old_data = data
        self.old_date_str = date_str    
            
        print(f"Timestep done in {(datetime.now(tz=self.timezone) - t_now).total_seconds():2.2f}s")
        return row_data

if __name__ == '__main__':
    pass