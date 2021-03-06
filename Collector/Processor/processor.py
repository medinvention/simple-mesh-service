import re
import os
import logging
import sys
import time
import mysql.connector
from datetime import datetime

connection = None
log = None

def connect():
        global connection
        if connection != None and connection.is_connected():
            return connection
        connection = None
        try:
            connection = mysql.connector.connect(
                host=os.environ["DB_HOST"], 
                database=os.environ["DB_NAME"], 
                user=os.environ["DB_USER"],
                password=os.environ["DB_PASSWORD"])
            if connection.is_connected():
                connection.autocommit = False
                return connection
            return None
        except RuntimeError as e:
            logger().error("Unable to connect to database : {}".format(e))
            return None

def run():
    tic = time.time()
    global connection 
    if not connect():
        raise NameError('Unable to connect to database')
    
    connection.start_transaction()
    logger().info("Start node processing...")
    processNode()
    
    cursor = connection.cursor()
    cursor.execute("SELECT * FROM access LIMIT 100")
    list = cursor.fetchall()
    columns = cursor.description
    processed, invalid, error = 0, 0, 0

    logger().info("Start request processing...")
    for value in list:
        row = associate(value, columns)
        state = processRequest(row)
        if state == True:
            processed += 1
        elif state == False:
            invalid += 1
        else:
            error += 1
    logger().info("Request processing finished with {} processed, {} invalid and on error {}".format(processed, invalid, error))

    # update node state (check if exist one active service)
    stateNode()
    
    toc = time.time()
    logger().info("Processor done in {} seconds.".format(round(toc -tic)))

def processRequest(request):
    global connection
    p = re.compile('([0-9.]+) - .* \[(.*)\] ".*" ([0-9]+) [0-9]+ ".*" ".*" - rt=([0-9\.]+) uct=[0-9\.]+ uht=[0-9\.]+ urt=([0-9\.]+)', re.IGNORECASE)
    r = p.match(request['message']);
    if r:
        try:
            toNode = getNodeIDByHostOrIp(request['host'])
            fromService = getServiceByHostOrIp(r.group(1))
            fromNode = getNodeIDByHostOrIp(r.group(1))
            # check if link from-to not found create if
            toID = toNode['id']
            fromID = fromService['id'] if fromService else 0 #0 if source is ingress
            fromNodeID = fromNode['id'] if fromNode else 0 #0 if source is ingress
            link = getLinkByFromAndToID(fromID, toID)
            if not link:
                linkID = createLink(fromNodeID, fromID, toID)
            else:
                linkID = link['id']
            # create request
            at = datetime.strptime(r.group(2), '%d/%b/%Y:%H:%M:%S %z')
            code = r.group(3)
            requestTime = r.group(4)
            responseTime = r.group(5)
            cursor = connection.cursor()
            cursor.execute("INSERT INTO request (link, from_id, to_id, code, at, request_time, response_time) VALUES (%s, %s, %s, %s, %s, %s, %s)", 
                (linkID, fromID, toID, code, at, requestTime, responseTime))
            cursor.execute("DELETE FROM access WHERE id = %s", (request['id'], ))
            connection.commit()
            return True
        except Exception as e:
            connection.rollback()
            logger().error("Error when request {} processing {}".format(request, e))
            cursor = connection.cursor()
            cursor.execute("INSERT INTO error (host, ident, message) VALUES (%s, 'processor', %s)", (request['host'], request['message']))
            cursor.execute("DELETE FROM access WHERE id = %s", (request['id'], ))
            connection.commit()
            return None
    else:
        logger().info("Invalid request {}".format(request))
        cursor = connection.cursor()
        cursor.execute("INSERT INTO error (host, ident, message) VALUES (%s, 'processor', %s)", (request['host'], request['message']))
        cursor.execute("DELETE FROM access WHERE id = %s", (request['id'], ))
        connection.commit()
        return False

def createLink(fromNodeID, fromID, toID):
    cursor = connection.cursor()
    cursor.execute("INSERT INTO link (from_node_id, from_id, to_id) VALUES (%s, %s, %s)", (fromNodeID, fromID,toID))
    connection.commit()
    logger().info("New link registred from Node {} to Node {}".format(fromNodeID, toID))
    return cursor.lastrowid

def getLinkByFromAndToID(fromID, toID):
    cursor = connection.cursor()
    cursor.execute("SELECT * FROM link WHERE from_id = %s and to_id = %s", (fromID,toID))
    link = cursor.fetchone()
    if not link:
        return None 
    return associate(link, cursor.description)

def getServiceByHostOrIp(id):
    cursor = connection.cursor()
    cursor.execute("SELECT * FROM registration WHERE host LIKE %s or ip LIKE %s", (id,id))
    registration = cursor.fetchone()
    if not registration:
        return None 
    return associate(registration, cursor.description)

def getNodeIDByHostOrIp(id):
    service = getServiceByHostOrIp(id)
    if not service:
        return None 
    return getNodeByGroupName(service['groupname'])

def processNode():
    global connection
    cursor = connection.cursor()
    cursor.execute("SELECT * FROM registration")
    list = cursor.fetchall()
    columns = cursor.description
    new, error = 0, 0
    for row in list:
        try:
            registration = associate(row, columns)
            node = getNodeByGroupName(registration['groupname'])
            if not node:
                createNode(registration)
                new += 1
        except Exception as e:
            connection.rollback()
            error += 1
            logger().error("Error when node {} processing {}".format(registration, e))
    logger().info("Node processing finished with {} created and on error {}".format(new, error))

def stateNode():
    global connection
    cursor = connection.cursor()
    cursor.execute("SELECT * FROM node")
    list = cursor.fetchall()
    columns = cursor.description
    up, error = 0, 0
    for row in list:
        try:
            node = associate(row, columns)
            if updateNode(node):
                up += 1
        except Exception as e:
            connection.rollback()
            error += 1
            logger().error("Error when node {} state updating {}".format(node, e))
    logger().info("Node state updating finished with {} updated and on error {}".format(up, error))

def createNode(registration):
    global connection
    cursor = connection.cursor()
    cursor.execute("INSERT INTO node (name, active) VALUES (%s, %s)", (registration['groupname'], False))
    connection.commit()
    logger().info("New node registred {}".format(registration['groupname']))
    return cursor.lastrowid

def updateNode(node):
    global connection
    cursor = connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM registration WHERE active = %s AND groupname = %s", (True, node['name']))
    count = cursor.fetchone()
    if (count[0] > 0 and not node['active']) or (count[0] == 0 and node['active']):
        cursor.execute("UPDATE node SET active = %s WHERE id = %s", (count[0] > 0, node['id']))
        connection.commit()
        logger().info("Node updated : {}".format(node['name']))
        return True   
    return False 

def getNodeByGroupName(groupname):
    cursor = connection.cursor()
    cursor.execute("SELECT * FROM node WHERE name LIKE %s", (groupname,))
    node = cursor.fetchone()
    if not node:
        return node 
    return associate(node, cursor.description)

def associate(data, columns):
    row = {}
    for (index,column) in enumerate(data):
        row[columns[index][0]] = column
    return row

def logger():
    global log
    if log != None:
        return log
    log = logging.getLogger(__name__)
    out_hdlr = logging.StreamHandler(sys.stdout)
    out_hdlr.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    out_hdlr.setLevel(logging.INFO)
    log.addHandler(out_hdlr)
    log.setLevel(logging.INFO)
    return log

if __name__ == '__main__':
    run()
