"""Response envelope and pagination recipes with project-declared JSON paths."""
_MISSING=object()
def field(value,path):
    for key in path:
        if isinstance(value,dict) and isinstance(key,str):value=value.get(key,_MISSING)
        elif isinstance(value,list) and type(key) is int and 0<=key<len(value):value=value[key]
        else:return _MISSING
    return value
class ConfiguredEnvelope:
    def __init__(self,*,success_path,success_value,data_path,code_path,message_path):
        self.success_path=success_path;self.success_value=success_value;self.data_path=data_path;self.code_path=code_path;self.message_path=message_path
    def is_ok(self,response):
        value=field(response,self.success_path)
        return type(value) is type(self.success_value) and value==self.success_value
    def is_reject(self,response,code=None):
        valid,_=self.validate_envelope(response)
        return valid and not self.is_ok(response) and (code is None or self.business_code(response)==code)
    def business_code(self,response):return self.normalize_code(field(response,self.code_path))
    def message(self,response):
        value=field(response,self.message_path);return value if isinstance(value,str) else ''
    def data(self,response):
        value=field(response,self.data_path);return None if value is _MISSING else value
    def normalize_code(self,value):return value if type(value) in (int,str) else None
    def validate_envelope(self,response):
        value=field(response,self.success_path)
        if type(value) is not type(self.success_value):return False,'declared success field missing or wrong JSON type'
        if self.is_ok(response) and field(response,self.data_path) is _MISSING:return False,'declared success payload missing'
        return True,'declared envelope valid'
class PageContract:
    def __init__(self,*,records_path,total_path):self.records_path=records_path;self.total_path=total_path
    def validate(self,response):
        rows=field(response,self.records_path);total=field(response,self.total_path);errors=[]
        if not isinstance(rows,list):errors.append('records must be an array')
        if type(total) is not int or total<0:errors.append('total must be a nonnegative JSON integer')
        elif isinstance(rows,list) and total<len(rows):errors.append('total smaller than returned record count')
        return errors
