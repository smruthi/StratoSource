#    Copyright 2010, 2011 Red Hat Inc.
#
#    This file is part of StratoSource.
#
#    StratoSource is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    StratoSource is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with StratoSource.  If not, see <http://www.gnu.org/licenses/>.
#    
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ObjectDoesNotExist
from stratosource.admin.models import Story, Branch, DeployableObject
from admin.management import Utils
import subprocess
import os
from zipfile import ZipFile
from lxml import etree


__author__="masmith"
__date__ ="$Sep 22, 2010 2:11:52 PM$"

typeMap = {'fields': 'CustomField','validationRules': 'ValidationRule',
           'listViews': 'ListView','namedFilters': 'NamedFilter',
           'searchLayouts': 'SearchLayout','recordTypes': 'RecordType',
           'objects': 'CustomObject'}
SF_NAMESPACE='{http://soap.sforce.com/2006/04/metadata}'
_API_VERSION = "23.0"


def createFileCache(map):
    cache = {}
    for type,list in map.items():
        if type == 'objects' or type == 'labels':
            for object in list:
                try:
                    path = os.path.join('unpackaged',type,object.filename)
                    f = open(path)
                    cache[object.filename] = f.read()
                    f.close()
                except IOError:
                    print '** not able to load ' + path
                    pass    # caused by a new file added, not present on current branch
        else:
            for object in list:
                path = os.path.join('unpackaged',type,object.filename)
                f = open(path)
                cache[object.filename] = f.read()
                f.close()
    return cache

def findXmlNode(doc, object):
    if object.type == 'objectTranslation':
        nodeName = SF_NAMESPACE + 'fields'
        nameKey = SF_NAMESPACE + 'name'
    if object.type == 'labels':
        nodeName = SF_NAMESPACE + 'labels'
        nameKey = SF_NAMESPACE + 'name'
    elif object.type == 'translationChange':
        nodeName = SF_NAMESPACE + 'fields'
        nameKey = SF_NAMESPACE + 'name'
    else:
        nodeName = SF_NAMESPACE + 'fields'
        nameKey = SF_NAMESPACE + 'fullName'

    children = doc.findall(nodeName)
    for child in children:
        node = child.find(nameKey)
        if node is not None:
            return etree.tostring(node)
    return None

#
# this is an exception to the usual pattern of 2-level-deep xml nodes
#
def findXmlSubnode(doc, object):
    if object.type == 'objects':
        node_names = object.el_name.split(':')
        print '>>> node_names[0] = %s, node_names[1] = %s' % (node_names[0], node_names[1])
        print '>>> subtype=' + object.el_subtype
        children = doc.findall(SF_NAMESPACE + object.el_type)
        print '>>> looking for ' + object.el_type
        for child in children:
            print '>>> processing child'
            node = child.find(SF_NAMESPACE + 'fullName')
            if node is not None:
                print '>>> processing node ' + node.text
                print '>>> comparing [%s] to [%s]' % (node.text, node_names[0])
                if node.text == node_names[0]:
                    if object.el_subtype == 'picklists':
                        plvalues = child.findall(SF_NAMESPACE + 'picklistValues')
                        for plvalue in plvalues:
                            print '>>> processing plvalue'
                            if plvalue.find(SF_NAMESPACE + 'picklist').text == node_names[1]:
                                print '>>> plvalue found'
                                #
                                # due to difficulty we have to return everything from the root node
                                #
                                return etree.tostring(child)
    else:
        print 'Unknown object type: ' + object.type
    return None

def generateObjectChanges(packageNode, destructiveNode, cache, object):

    fragment = []
    if object.status == 'd':
        destructiveTypesEl = etree.SubElement(destructiveNode, 'types')
        object_name = object.filename[:-(len(object.el_type) + 1)]
        member = '.'.join([object_name, object.el_name ])
        etree.SubElement(destructiveTypesEl, 'members').text = member
        etree.SubElement(destructiveTypesEl, 'name').text = typeMap[object.type]
        return fragment

    doc = etree.XML(cache[object.filename])

    if object.el_name.find(':') >= 0:
        # recordType node
        xml = findXmlSubnode(doc, object)
    else:
        xml = findXmlNode(doc, object)

    if not xml:
        print "Did not find XML node for %s.%s.%s.%s" % (object.filename,object.el_type,object.el_name,object.el_subtype)
    else:
        fragment.append(xml)
    return fragment


def getMetaForFile(filename):
    with file(filename+'-meta.xml') as f:
        return f.read()

    
def generatePackage(objectList, from_branch, to_branch):
    typemap = { 'classes' : 'ApexClass', 'labels' : 'CustomLabel',
                'objects': 'CustomObject', 'triggers': 'ApexTrigger',
                'pages': 'ApexPage', 'weblinks': 'CustomPageWebLink',
                'components': 'ApexComponent' }

    defaultNS = { None: 'http://soap.sforce.com/2006/04/metadata'}
    doc = etree.Element('Package', nsmap=defaultNS)
    etree.SubElement(doc, 'version').text = "{0}".format(_API_VERSION)

    destructive = etree.Element('Package', nsmap=defaultNS)
    etree.SubElement(destructive, 'version').text = "{0}".format(_API_VERSION)

    output_name = '/tmp/deploy_%s_%s.zip' % (from_branch.name, to_branch.name)
    myzip = ZipFile(output_name, 'w')

    # create map and group by type
    map = {}
    for object in objectList:
        if not map.has_key(object.type): map[object.type] = []
        map[object.type].append(object)
    cache = createFileCache(map)

    for type,itemlist in map.items():
        if not typemap.has_key(type):
            print '** Unhandled type {0} - skipped'.format(type)
            continue

        print 'PROCESSING TYPE ' + type

        if type == 'objects':
            objectxml = '<?xml version="1.0" encoding="UTF-8"?>'\
                        '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">'
            for object in itemlist:
                if object.el_name is None:
                    ##
                    # entire object is new, handle differently
                    ##
                    myzip.writestr('objects/'+object.filename, cache.get(object.filename))
                    typesEl = etree.SubElement(doc, 'types')
                    etree.SubElement(typesEl, 'members').text = '*'
                    etree.SubElement(typesEl, 'name').text = typemap[type]
                else:
                    ##
                    # exists in both hashes, so compare for changes
                    ##
                    fragments = generateObjectChanges(doc, destructive, cache, object)
                    objectxml += '\n'.join(fragments)
                    objectxml += '</CustomObject>'
                    myzip.writestr('objects/'+object.filename, objectxml)
        elif type == 'labels':
            objectxml = ''
            for objectName in itemlist:
                fragments = generateObjectChanges(doc, destructive, lFileCache, rFileCache, itemlist, 'labels', 'CustomLabel')
                myzip.writestr('labels/'+objectName, objectxml)
        else:
            typesEl = etree.SubElement(doc, 'types')
            destructiveList = []
            for member in itemlist:
                print 'member filename=%s, el_type=%s' % (member.filename, member.el_type)
                if member.filename.find('.') > 0:
                    object_name = member.filename[0:member.filename.find('.')]
#                object_name = member.filename[:-(len(member.el_type) + 1)]
                ## !! assumes the right-side branch is still current in git !!
                if os.path.isfile(os.path.join('unpackaged',type,member.filename)):
                    myzip.writestr(type+'/'+member.filename, cache.get(member.filename))
                    myzip.writestr(type+'/'+member.filename+'-meta.xml', getMetaForFile(os.path.join('unpackaged',type,member.filename)))
                    etree.SubElement(typesEl, 'members').text = object_name
                else:
                    destructiveList.append(member)

            etree.SubElement(typesEl, 'name').text = typemap[type]
            if len(destructiveList):
                destructiveTypesEl = etree.SubElement(destructive, 'types')
                for member in destructiveList:
                    if member.filename.find('.') > 0:
                        object_name = member.filename[0:member.filename.find('.')]
#                    object_name = member.filename[:-(len(member.el_type) + 1)]
                    etree.SubElement(destructiveTypesEl, 'members').text = object_name;
                etree.SubElement(destructiveTypesEl, 'name').text = typemap[type]

    xml = etree.tostring(doc, xml_declaration=True, encoding='UTF-8', pretty_print=True)
    myzip.writestr('package.xml', xml)

    xml = etree.tostring(destructive, xml_declaration=True, encoding='UTF-8', pretty_print=True)
    myzip.writestr('destructiveChanges.xml', xml)
    myzip.close()
    return output_name


class Command(BaseCommand):

    def deploy(self, objectList, from_branch, to_branch):
        for object in objectList:
            print object.status, object.filename, object.type, object.el_name, object.el_subtype
        output_name = generatePackage(objectList, from_branch, to_branch)
        agent = Utils.getAgentForBranch(to_branch);
        agent.deploy(output_name)

    def deploy_story(self, story, from_branch, to_branch):
        # get all release objects associated with our story
        rolist = DeployableObject.objects.filter(pending_stories=story)
        resetLocalRepo(from_branch.name)
        self.deploy(set(rolist), from_branch, to_branch)

    def handle(self, *args, **options):

        if len(args) < 6: raise CommandError('usage: deploy <source repo> <source branch> <dest repo> <dest branch> story <storyid>')

        if args[4] == 'story':
            story = Story.objects.get(rally_id=args[5])
            if not story: raise CommandException("invalid story")
            from_branch = Branch.objects.get(repo__name__exact=args[0], name__exact=args[1])
            if not from_branch: raise CommandException("invalid source branch")
            to_branch = Branch.objects.get(repo__name__exact=args[2], name__exact=args[3])
            if not to_branch: raise CommandException("invalid destination branch")
            os.chdir(from_branch.repo.location)
            self.deploy_story(story, from_branch, to_branch)



def resetLocalRepo(branch_name):
    subprocess.check_call(["git","checkout",branch_name])
#    subprocess.check_call(["git","reset","--hard","{0}".format(branch_name)])

