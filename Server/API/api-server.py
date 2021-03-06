from flask import Flask, json, request
from flask_jwt_extended import (
    JWTManager, jwt_required, create_access_token,
    jwt_refresh_token_required, create_refresh_token,
    get_jwt_identity
)
from flask_cors import CORS, cross_origin
from werkzeug.security import safe_str_cmp
from mysql.connector import Error
import mysql.connector
import os

if not os.environ.get("USERNAME") or not os.environ.get("PASSWORD"):
    raise RuntimeError('Username and password environment variables must be defined for security access')

api = Flask(__name__)
api.debug = True if os.environ.get("DEBUG") else False
api.config['JWT_SECRET_KEY'] = os.environ.get("JWT_SECRET") if os.environ.get("JWT_SECRET") else 'static-jwt-secret'
api.config['CORS_HEADERS'] = 'Content-Type'
cors = CORS(api)
jwt = JWTManager(api)

@cross_origin()
@api.route('/get', methods=['OPTIONS'])
def authOption():
    return 'OPTIONS'

@cross_origin()
@api.route('/auth', methods=['POST'])
def auth():
    username = request.json.get('username', None)
    password = request.json.get('password', None)
    if not safe_str_cmp(os.environ.get("USERNAME").encode('utf-8'), username.encode('utf-8')):
        return json.dumps({"msg": "Bad credentials"}), 401
    if not safe_str_cmp(os.environ.get("PASSWORD").encode('utf-8'), password.encode('utf-8')):
        return json.dumps({"msg": "Bad credentials"}), 401
    return json.dumps({
        'access_token': create_access_token(identity=username),
        'refresh_token': create_refresh_token(identity=username)
    }), 200

@cross_origin()
@api.route('/refresh', methods=['OPTIONS'])
def refreshOption():
    return 'OPTIONS'

@cross_origin()
@api.route('/refresh', methods=['POST'])
@jwt_refresh_token_required
def refresh():
    user = get_jwt_identity()
    return json.dumps({
        'access_token': create_access_token(identity=user)
    }), 200

@cross_origin()
@api.route('/get', methods=['OPTIONS'])
def getOption():
    return 'OPTIONS'
    
@cross_origin()
@api.route('/get', methods=['GET'])
@jwt_required
def get():
    fromDate = request.args.get('from', None)
    toDate = request.args.get('to', None)
    filtredNamespace = request.args.get('namespace', None)

    data = {'ingress': False, 'nodes': [], 'links': [], 'from': fromDate, 'to': toDate, 'namespace': filtredNamespace}

    db = connect()
    if db == None:
        return json.dumps({"status": False, "message": "Unable to connect to master db"})
        
    cursor = db.cursor()
    cursor.execute("SELECT * FROM node")
    list = cursor.fetchall()
    columns = cursor.description
    serviceIds = ['0']
    for row in list:
        node = associate(row, columns)

        metadata = getMetadata(node)
        services = getService(node, filtredNamespace)
        trafic = getTrafic(node, services, fromDate, toDate)
        status = getStatus(node, services, fromDate, toDate)
        
        serviceIds += map(lambda d: str(d['id']), services)
        # skeep node without service
        if len(services) > 0:
            data['nodes'].append({
                'id': node['id'], 
                'name': node['name'], 
                'disabled': False if 1 == node['active'] else True,
                'services': services, 
                'metadata': metadata,
                'trafic' : trafic,
                'status': status})

    if not len(serviceIds) or not len(data['nodes']):
        return json.dumps(data)

    nodeIds = map(lambda d: str(d['id']), data['nodes'])
    cursor.execute("SELECT * FROM link WHERE from_id IN ("+','.join(serviceIds)+ ") AND to_id IN ("+','.join(nodeIds)+ ")")
    list = cursor.fetchall()
    columns = cursor.description
  
    for row in list:
        link = associate(row, columns)
        if int(link['from_node_id']) == 0:
            data['ingress'] = True
        data['links'].append({
            'from': str(link['from_node_id'])+'#'+str(link['from_id']) if int(link['from_node_id']) != 0 else 'ingress', 
            'to': str(link['to_id'])})

    db.close()        
    return json.dumps(data)

def getStatus(node, services, fromDate, toDate):
    db = connect()
    cursor = db.cursor()
    inQuery = """SELECT COUNT(*) AS count , 
        SUM(CASE WHEN SUBSTRING(code, 1, 1) = '2' THEN 1 ELSE 0 END) AS 2xx, 
        SUM(CASE WHEN SUBSTRING(code, 1, 1) = '3' THEN 1 ELSE 0 END) AS 3xx,
        SUM(CASE WHEN SUBSTRING(code, 1, 1) = '4' THEN 1 ELSE 0 END) AS 4xx, 
        SUM(CASE WHEN SUBSTRING(code, 1, 1) = '5' THEN 1 ELSE 0 END) AS 5xx
        FROM request WHERE to_id = %s
    """
    inQueryParams = (node['id'],)
    if fromDate != None and fromDate != '':
        inQuery += " AND created_at > %s"
        inQueryParams += (fromDate,)
    if toDate != None and toDate != '':
        inQuery += " AND created_at < %s "
        inQueryParams += (toDate,)
    cursor.execute(inQuery, inQueryParams)

    row = cursor.fetchone()
    columns = cursor.description
    statusIn = associate(row, columns)

    serviceIds = []
    for service in services:
        serviceIds.append(str(service['id']))
    
    if not len(serviceIds):
        return {}

    outQuery = """SELECT COUNT(*) AS count , 
        SUM(CASE WHEN SUBSTRING(code, 1, 1) = '2' THEN 1 ELSE 0 END) AS 2xx, 
        SUM(CASE WHEN SUBSTRING(code, 1, 1) = '3' THEN 1 ELSE 0 END) AS 3xx,
        SUM(CASE WHEN SUBSTRING(code, 1, 1) = '4' THEN 1 ELSE 0 END) AS 4xx, 
        SUM(CASE WHEN SUBSTRING(code, 1, 1) = '5' THEN 1 ELSE 0 END) AS 5xx
        FROM request WHERE from_id IN ("""+','.join(serviceIds)+ """)
    """
    outQueryParams = ()
    if fromDate != None and fromDate != '':
        outQuery += " AND created_at > %s"
        outQueryParams += (fromDate,)
    if toDate != None and toDate != '':
        outQuery += " AND created_at < %s "
        outQueryParams += (toDate,)

    cursor.execute(outQuery, outQueryParams)
    row = cursor.fetchone()
    columns = cursor.description
    statusOut = associate(row, columns)
    
    inState = {}
    index = ['2xx', '3xx', '4xx', '5xx']
    for column in index:
        inState[column] = round(100 * int(statusIn[column]) / int(statusIn['count'])) if statusIn['count'] and int(statusIn['count']) > 0 else 0
    outState = {}
    for column in index:
        outState[column] = round(100 * int(statusOut[column]) / int(statusOut['count'])) if statusOut['count'] and int(statusOut['count']) > 0 else 0
    return {'in' : inState, 'out': outState} 

def getTrafic(node, services, fromDate, toDate):
    db = connect()
    cursor = db.cursor()

    inTimeQuery = "SELECT AVG(request_time) FROM request WHERE to_id = %s"
    inTimeQueryParams = (node['id'],)
    if fromDate != None and fromDate != '':
        inTimeQuery += " AND created_at > %s "
        inTimeQueryParams += (fromDate,)
    if toDate != None and toDate != '':
        inTimeQuery += " AND created_at < %s "
        inTimeQueryParams += (toDate,)
    cursor.execute(inTimeQuery, inTimeQueryParams)
    inTime = cursor.fetchone()

    outTimeQuery = "SELECT AVG(response_time) FROM request WHERE to_id = %s"
    outTimeQueryParams = (node['id'],)
    if fromDate != None and fromDate != '':
        outTimeQuery += " AND created_at > %s  "
        outTimeQueryParams += (fromDate,)
    if toDate != None and toDate != '':
        outTimeQuery += " AND created_at < %s  "
        outTimeQueryParams += (toDate,)
    cursor.execute(outTimeQuery, outTimeQueryParams)
    outTime = cursor.fetchone()

    inQuery = """SELECT COUNT(*) AS count , SUM(CASE WHEN SUBSTRING(code, 1, 1) != '5' THEN 1 ELSE 0 END) 
        AS success, SUM(CASE WHEN SUBSTRING(code, 1, 1) = '5' THEN 1 ELSE 0 END) AS error 
        FROM request WHERE to_id = %s
    """
    inQueryParams = (node['id'],)
    if fromDate != None and fromDate != '':
        inQuery += " AND created_at > %s  "
        inQueryParams += (fromDate,)
    if toDate != None and toDate != '':
        inQuery += " AND created_at < %s  "
        inQueryParams += (toDate,)

    cursor.execute(inQuery, inQueryParams)
    row = cursor.fetchone()
    columns = cursor.description
    stateIn = associate(row, columns)

    serviceIds = []
    for service in services:
        serviceIds.append(str(service['id']))

    if not len(serviceIds):
        return { } 

    outQuery = """SELECT COUNT(*) AS count , SUM(CASE WHEN SUBSTRING(code, 1, 1) != '5' THEN 1 ELSE 0 END) 
        AS success, SUM(CASE WHEN SUBSTRING(code, 1, 1) = '5' THEN 1 ELSE 0 END) AS error 
        FROM request WHERE from_id IN (""" + ','.join(serviceIds) +""")
    """
    outQueryParams = ()
    if fromDate != None and fromDate != '':
        outQuery += " AND created_at > %s "
        outQueryParams += (fromDate,)
    if toDate != None and toDate != '':
        outQuery += " AND created_at < %s "
        outQueryParams += (toDate,)
        
    cursor.execute(outQuery, outQueryParams)
    row = cursor.fetchone()
    columns = cursor.description
    stateOut = associate(row, columns)
    return {
        'in' : {
            'time': round(inTime[0] if inTime[0] else 0, 3), 
            'success': round(100 * int(stateIn['success']) / int(stateIn['count'])) if int(stateIn['count']) > 0 else 0, 
            'error': round(100 * int(stateIn['error']) / int(stateIn['count'])) if int(stateIn['count']) > 0 else 0},
        'out' : {
            'time': round(outTime[0] if outTime[0] else 0, 3), 
            'success': round(100 * int(stateOut['success']) / int(stateOut['count'])) if int(stateOut['count']) > 0 else 0, 
            'error': round(100 * int(stateOut['error']) / int(stateOut['count'])) if int(stateOut['count']) > 0 else 0}
    } 

def getService(node, filtredNamespace):
    db = connect()
    services = []
    cursor = db.cursor()
    if(filtredNamespace != None and filtredNamespace != ''):
        cursor.execute("SELECT * FROM registration WHERE groupname = %s AND namespace = %s AND active = 1", (node['name'],filtredNamespace))
    else :
        cursor.execute("SELECT * FROM registration WHERE groupname = %s AND active = 1", (node['name'],))
    list = cursor.fetchall()
    columns = cursor.description
    for row in list:
        service = associate(row, columns)
        services.append({
            'name': service['service'] if service['service'] else service['pod'] ,
            'id': service['id'],
            'host': service['host'],
            'pod': service['pod'],
            'ip': service['ip'],
            'port': service['port'],
            'namespace': service['namespace']
        })
    return services

def getMetadata(node):
    return [
        {'name': 'Group ID', 'value' : '#'+str(node['id'])},
        {'name': 'Group name', 'value' : node['name']},
        {'name': 'Creation date', 'value' : node['created_at']}
    ]

def connect():
    try:
        conn = mysql.connector.connect(host=os.environ["DB_HOST"], database=os.environ["DB_NAME"], user=os.environ["DB_USER"],password=os.environ["DB_PASSWORD"])
        if conn.is_connected():
            return conn
        return None
    except Error as e:
        return None

def associate(data, columns):
    row = {}
    for (index,column) in enumerate(data):
        row[columns[index][0]] = column
    return row


if __name__ == '__main__':
    api.run('0.0.0.0')